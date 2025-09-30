r"""
An interactive release scrapper for repositories on Github.

Use Python 12+. Ensure `_toml_validation.py` is in the same folder.
Install pre-requisites:

    pip install -U PyGithub tomli-w

Place a `github_releases_dl.toml` in the same directory, with
configuration of what to do:

    # query releases only up to a release with an older date: works faster
    assume_releases_decreasing = true
    # faulty (uses commitish without resolving) but you can use it if dates don't work
    compare_commits = false
    # your Github access token; if possible use a new one with no additional rights set
    api_token = 'YOUR_GITHUB_ACCESS_TOKEN'
    
    # you can query releases granularly by dividing them into groups
    # otherwise just define a single group
    [group.GROUPNAME]        # beware the name is case-sensitive: probably do lowercase
    # downloads get placed into this subfolder; groups can freely share folders
    folder = "RELATIVE PATH"
    
    [[group.GROUPNAME.repos]]   # for each repo in this group
    id = "USER/REPONAME"        # obviously, its id
    # regex matchers: only releases matching these are shown
    # normally it's assumed each should have just a single match
    # if the list is empty or this key absent, every release is shown
    # note: by default, `(?a)` mode is active, so `\d` is strictly `[0-9]` etc
    matchers = [ 'REGEX1', 'REGEX2' ]

The script then creates a file `github_releases_dl.cache.toml` to remember dates
of the releases you downloaded.

Don't forget you can specify several groups or release assets when asked, separated by spaces.
Inputting nothing will assume you want nothing. Inputting `*` will pick everything.

There's no download progress bar right now, sorry. But if any downloads were made,
corresponding folder windows will be opened for you.
"""

from __future__ import annotations
import argparse
from dataclasses import dataclass
from datetime import datetime
from functools import cache, cached_property
from os import fspath, startfile
from pathlib import Path
import re
from time import sleep
import tomllib
from typing import Final, Literal, cast, final, TypedDict, overload

from github import Github, Auth  # pip install -U PyGithub
from github.GitRelease import GitRelease
from github.GithubException import UnknownObjectException
from github.GitReleaseAsset import GitReleaseAsset
import tomli_w  # pip install -U tomli-w

from _toml_validation import toml_check, toml_check_get, toml_check_seq


API_SLEEP_SEC: Final[float] = 0.005  # ≥0.005 worked last time
MAX_BODY_LEN: Final[int] = 80  # in printing
DL_CHUNK_SIZE: Final[int] = 256 * 1024  # 256 KiB


@cache
def _get_script_filepath() -> Path:
    from inspect import currentframe, getframeinfo
    assert (cur_frame := currentframe())
    return Path(getframeinfo(cur_frame).filename).resolve()

def get_script_dir() -> Path:
    return _get_script_filepath().parent

def get_config_path() -> Path:
    return _get_script_filepath().with_suffix('.toml')

def get_cache_path() -> Path:
    return _get_script_filepath().with_suffix('.cache.toml')


class CacheRepo(TypedDict):
    # id: str  # KEY
    last_release_commit: str
    last_release_date: datetime

class CacheRoot(TypedDict):
    repos: dict[str, CacheRepo]

@final
class Settings:
    def __init__(self) -> None:
        self._source_path: Path | None = None
        self._cache: CacheRoot | None = None

    @cached_property
    def config(self) -> Config:
        with open(get_config_path(), 'rb') as f:
            return Config.import_toml(tomllib.load(f))

    def _get_cache(self) -> CacheRoot:
        if self._cache:
            return self._cache
        raise ValueError('cache was not loaded')

    def update_cache(self, rel: ReleaseData) -> None:
        self._get_cache()['repos'][rel.repo_id] = {
            'last_release_commit': rel.commit,
            'last_release_date': rel.last_modified,
        }

    def get_cached(self, repo_id: str) -> CacheRepo:
        return self._get_cache()['repos'].get(repo_id) or {
            'last_release_commit': '',
            'last_release_date': datetime.fromisoformat('0001-01-01'),
        }

    def connect_cache(self) -> None:
        path = get_cache_path()
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        with open(path, 'rb') as f:
            raw_cache = tomllib.load(f)
        if not raw_cache:  # fresh one
            raw_cache['repos'] = {}
        repos = toml_check_get(raw_cache, 'repos', dict, '')
        for _, repo in repos.items():
            repo = toml_check(repo, dict, 'repos[N]')
            toml_check_get(repo, 'last_release_commit', str, 'repos[N]')
            toml_check_get(repo, 'last_release_date', datetime, 'repos[N]')
        self._cache = cast('CacheRoot', raw_cache)

    def save(self) -> None:
        data = self._get_cache()
        with open(get_cache_path(), 'wb') as f:
            tomli_w.dump(data, f)


@dataclass(frozen=True, slots=True)
class Config:
    assume_releases_decreasing: bool
    compare_commits: bool
    api_token: str
    downloads_root: Path
    groups: tuple[GroupSpec, ...]

    @staticmethod
    def import_toml(root: dict[str, object]) -> Config:
        assume_rd = toml_check_get(root, 'assume_releases_decreasing', bool | None, '')
        if assume_rd is None:
            assume_rd = True
        compare_commits = toml_check_get(root, 'compare_commits', bool | None, '')
        if compare_commits is None:
            compare_commits = False
        api_token = toml_check_get(root, 'api_token', str, '')
        dl_root = Path(toml_check_get(root, 'downloads_root', str | None, '') or '')
        dl_root = (get_script_dir() / dl_root).resolve()
        group_data = toml_check_get(root, 'group', dict, '')
        return Config(
            assume_releases_decreasing=assume_rd,
            compare_commits=compare_commits,
            api_token=api_token,
            downloads_root=dl_root,
            groups=GroupSpec.import_toml_many(group_data))


@dataclass(frozen=True, slots=True)
class GroupSpec:
    id: str
    folder: str
    repos: tuple[RepoSpec, ...]

    def __post_init__(self) -> None:
        if re.search(r'\s', self.id):
            raise ValueError('id should contain no whitespace')
        # and can't check for valid paths regarding `self.folder`

    @staticmethod
    def import_toml_many(data: dict[str, object]) -> tuple[GroupSpec, ...]:
        result = []
        for key, value in data.items():
            value = toml_check(value, dict, 'group.X')
            result.append(GroupSpec.import_toml(key, value))
        return tuple(result)

    @staticmethod
    def import_toml(key: str, value: dict[str, object]) -> GroupSpec:
        folder = toml_check_get(value, 'folder', str, 'group.X')
        repos = toml_check_get(value, 'repos', list, 'group.X')
        repos = tuple(map(RepoSpec.import_toml, repos))
        return GroupSpec(id=key, folder=folder, repos=repos)


@dataclass(frozen=True, slots=True)
class RepoSpec:
    author: str
    name: str
    matchers: tuple[re.Pattern[str], ...]

    def __post_init__(self) -> None:
        if r'/' in self.author:
            raise ValueError('author should not contain "/"')
        if r'/' in self.name:
            raise ValueError('name should not contain "/"')
        if not self.matchers:
            raise ValueError('there should be at least a single matcher, like ".+"')

    @staticmethod
    def import_toml(value: object) -> RepoSpec:
        value = toml_check(value, dict, 'group.X.repos[N]')
        id_ = toml_check_get(value, 'id', str, 'group.X.repos[N]')
        id_parts = id_.split('/')
        if len(id_parts) != 2:
            raise ValueError('`group.X.repos[N].id` should be of form "author/name"')
        matchers = toml_check_get(value, 'matchers', list | None, 'group.X.repos[N]') or []
        if not matchers:
            matchers.append(r'.+')
        matchers = toml_check_seq(matchers, lambda x: toml_check(x, str, 'group.X.repos[N].matchers[N]'))
        re_flags = re.ASCII # | re.IGNORECASE  # re.ASCII is for \d ≡ [0-9]
        return RepoSpec(author=id_parts[0], name=id_parts[1],
                        matchers=tuple(re.compile(x, re_flags) for x in matchers))

    def id(self) -> str:
        return fr'{self.author}/{self.name}'


@dataclass(frozen=True, slots=True)
class ReleaseData:
    raw: GitRelease
    repo_id: str
    last_modified: datetime
    commit: str
    matched_assets: dict[str, list[GitReleaseAsset]]  # every matched


class Work:
    def __init__(self) -> None:
        self.settings = Settings()
        self._github: Github | None = None

    def auth(self) -> None:
        if self._github:
            raise ValueError('already authorized')
        auth = Auth.Token(self.settings.config.api_token)
        self._github = Github(auth=auth)

    def mark_release(self, release: ReleaseData) -> None:
        # save this to the settings
        self.settings.update_cache(release)
        self.settings.save()

    def download_asset(self, asset: GitReleaseAsset, dl_path: str) -> None:
        sleep(API_SLEEP_SEC)
        asset.download_asset(dl_path, DL_CHUNK_SIZE)

    def releases(self, repo: RepoSpec) -> list[ReleaseData]:
        if not self._github:
            self.auth()
            assert self._github
        CONFIG: Final = self.settings.config
        sleep(API_SLEEP_SEC)
        try:
            gh_repo = self._github.get_user(repo.author).get_repo(repo.name)
        except UnknownObjectException as exc:
            raise ValueError(f'repo is not found: {repo.id()}') from exc
        releases = gh_repo.get_releases()
        cached = self.settings.get_cached(repo.id())
        releases_out: list[ReleaseData] = []
        last_release_date: datetime | None = None
        for rel in releases:
            sleep(API_SLEEP_SEC)
            rel.complete()
            last_modified = rel.last_modified_datetime
            assert last_modified is not None
            commit = rel.target_commitish
            if last_release_date and last_modified < last_release_date:
                # this is definitely older than we're interested in
                if CONFIG.assume_releases_decreasing:
                    break
                continue
            if last_modified == cached['last_release_date']:
                # we found the date, can filter everything not-newer incl. this
                last_release_date = last_modified
                if CONFIG.assume_releases_decreasing:
                    break
                continue
            if CONFIG.compare_commits and commit == cached['last_release_commit']:
                if last_release_date is None:
                    print(f'[date didn\'t match but commit matched: {rel.name}, {rel.tag_name}, {rel.last_modified_datetime}]')
                    last_release_date = last_modified
                    if CONFIG.assume_releases_decreasing:
                        break
                    continue
            matched_assets: dict[str, list[GitReleaseAsset]]
            matched_assets = {regex.pattern: [] for regex in repo.matchers}
            for asset in rel.assets:
                sleep(API_SLEEP_SEC)
                for regex in repo.matchers:
                    if regex.search(asset.name):
                        matched_assets[regex.pattern].append(asset)
            releases_out.append(ReleaseData(
                raw=rel,
                repo_id=repo.id(),
                last_modified=last_modified,
                commit=commit,
                matched_assets=matched_assets,
            ))

        if last_release_date:
            second_pass = False
            for i in range(len(releases_out))[::-1]:
                if releases_out[i].last_modified < last_release_date:
                    del releases_out[i]
                    second_pass = True
            if second_pass:
                print('[!! there was second pass of pruning older releases]')
        elif releases_out:
            print('[last release date not found in current releases]')

        # return everything new, newest first, oldest last
        releases_out.sort(key=lambda rel: rel.last_modified, reverse=True)
        return releases_out


def maybe_int(x: str, range_: range | None = None) -> int | None:
    try:
        result = int(x)
    except ValueError:
        return None
    if range_ is not None and result not in range_:
        return None
    return result


@overload
def input_int(prompt: str, range_: range, allow_n: Literal[False]) -> int:
    ...
@overload
def input_int(prompt: str, range_: range, allow_n: Literal[True]) -> int | None:
    ...
def input_int(prompt: str, range_: range, allow_n: bool = False) -> int | None:
    result: int | None = None
    while result is None:
        answer = input(prompt).strip()
        if allow_n and answer.lower() in ('', 'n'):
            return None
        result = maybe_int(answer, range_)
        if result is None:
            print(f'[not an integer in {range_[0]}..{range_[-1]}]')
    return result


@dataclass(slots=True)  # `slots` allows us to check what arguments we recieve
class CliArgs:
    subcommand: Literal['auto', None] = None
    groups: list[str] | None = None


def define_arg_parser(config: Config) -> argparse.ArgumentParser:
    group_ids_set = frozenset(g.id for g in config.groups)
    def group(value: str) -> str:
        if value in group_ids_set or value == '*':
            return value
        raise ValueError(f'not a group I recognize ({", ".join(group_ids_set)})')

    parser = argparse.ArgumentParser(
        description='Release downloader from Github repos',
        allow_abbrev=False)
    subparsers = parser.add_subparsers(
        dest='subcommand',
        title='subcommands', help='specify none to run in manual mode')
    parser_auto = subparsers.add_parser(
        'auto', help='automatically download newest releases of everything')
    parser_auto.add_argument(
        'groups', nargs='+', metavar='group', type=group,
        help='repo groups to download from, or "*" to get everything')
    return parser


def main() -> None:
    work = Work()
    settings = Settings()
    work.settings = settings
    config = settings.config
    settings.connect_cache()
    parser = define_arg_parser(config)

    cli = parser.parse_args(namespace=CliArgs())
    AUTO: Final[bool] = cli.subcommand == 'auto'

    # TODO make body multiline!!!! use pprint or something!

    print(f'I will download into "{fspath(config.downloads_root)}"')
    if AUTO:
        assert cli.groups
        if cli.groups == ['*']:
            picked_groups = config.groups
        else:
            picked_groups = tuple(g for g in config.groups if g.id in cli.groups)
            if '*' in cli.groups:
                print('[* found among real group names and ignored]')
    else:
        picked_groups = pick_groups(config)
    if not picked_groups:
        print('Nothing selected. Bye!')
        return

    download_folders: set[str] = set()
    print()
    for group in picked_groups:
        for repo in group.repos:
            if todo_refactor_process_repo(work, group, repo, AUTO):
                download_folders.add(group.folder)
            print()

    if download_folders:
        print('Opening download folders...')
        for folder in download_folders:
            startfile(config.downloads_root / folder)


def pick_groups(config: Config) -> tuple[GroupSpec, ...]:
    ids_defined = [g.id for g in config.groups]
    ids_defined_set = frozenset(ids_defined)
    print(f'Groups to search in:\n  {" ".join(ids_defined)}')
    incorrect = True
    while incorrect:
        answer = input('Pick some (space-delimited) or * (all): ')
        id_list = answer.strip().split()
        if not id_list:
            return ()
        if id_list == ['*']:
            return config.groups
        incorrect = False
        for id_ in id_list:
            if not id_ in ids_defined_set:
                incorrect = True
                print(f'[unknown group: {id_}]')
    return tuple(g for g in config.groups if g.id in frozenset(id_list))


def todo_refactor_process_repo(work: Work, group: GroupSpec, repo: RepoSpec,
                               auto_mode: bool) -> bool:
    print(f'******* {repo.author} / {repo.name}')
    releases = work.releases(repo)
    if not releases:
        print('  no newer releases found!')
        return False
    print(f'  newer releases: {len(releases)}')

    for rel_idx, rel in enumerate(releases, start=1):
        last_modified_str = rel.last_modified.isoformat(' ', 'seconds')
        print(f'    [{rel_idx}] {last_modified_str}  {rel.raw.name}')
        body = rel.raw.body
        body = re.sub(r'(?m)[\r\n]+', ' ', body)
        if len(body) > MAX_BODY_LEN:
            body = body[:MAX_BODY_LEN - 1] + '…'
        print(f'      tag:{rel.raw.tag_name} pre:{rel.raw.prerelease}')
        print(f'      "{body}"')

        print('      Assets matched:')
        asset_idx = 1
        for regex, assets in rel.matched_assets.items():
            print(f'      for `{regex}`:', end='')
            if len(assets) == 1:
                print(f'\n        [{asset_idx}] "{assets[0].name}"')
                asset_idx += 1
            elif len(assets) > 1:
                print(' COLLISION')
                for asset_name in assets:
                    print(f'        [{asset_idx}] "{asset_name.name}"')
                    asset_idx += 1
            else:
                print(' NOTHING')

        if rel_idx != len(releases):
            if auto_mode:
                break
            answer = input('Show more releases? [y/N] ')
            if answer.strip().lower() != 'y':
                break
        else:
            print('  ===== no more releases ====\n')

    if auto_mode:
        rel = releases[0]
    else:
        rel_idx = input_int('Choose a release index to download and remember? [or N] ',
                            range(1, len(releases) + 1), allow_n=True)
        if rel_idx is None:
            return False
        rel = releases[rel_idx - 1]
    work.mark_release(rel)

    if all(len(asset_group) <= 1 for asset_group in rel.matched_assets.values()):
        if not auto_mode:
            print('[each regex matched <= 1 assets, ok to *]')
    else:
        print('[some regexes had multiple matches, beware!!]')

    if auto_mode:
        # everything!
        dl_assets = [each for many in rel.matched_assets.values() for each in many]
    else:
        dl_assets = ask_for_assets(rel)
    if not dl_assets:
        return False

    DL_FOLDER: Final = work.settings.config.downloads_root / group.folder
    DL_FOLDER.mkdir(exist_ok=True)
    downloaded_anything: bool = False
    for asset in dl_assets:
        if download_asset(asset, work, DL_FOLDER):
            downloaded_anything = True
    return downloaded_anything


def ask_for_assets(rel: ReleaseData) -> list[GitReleaseAsset]:
    flat_assets = [each for many in rel.matched_assets.values() for each in many]
    while True:
        answer = input('Enter asset indices to download, or * for everything: ')
        answer = answer.strip().lower()
        if not answer:
            print('[alright, no downloads]')
            return []
        elif answer == '*':
            return [asset for asset_group in rel.matched_assets.values()
                          for asset in asset_group]
        else:
            range_ = range(1, len(flat_assets) + 1)
            int_pieces = [maybe_int(x, range_) for x in answer.split()]
            if any(x is None for x in int_pieces):
                print(f'[some indices aren\'t in {range_[0]}..{range_[-1]}]')
                continue
            int_pieces = cast('list[int]', int_pieces)
            return [flat_assets[i - 1] for i in int_pieces]


def download_asset(asset: GitReleaseAsset, work: Work, directory: Path) -> bool:
    size, size_unit = asset.size, 'B'
    for unit in ('KiB', 'MiB', 'GiB', 'TiB'):
        if size > 1000:
            size /= 1024
            size_unit = unit
    print(f'Downloading {asset.name} ({size:.4g} {size_unit})...', end='', flush=True)
    dl_path: Final = directory / asset.name
    work.download_asset(asset, fspath(dl_path))  # TODO make it more interactive?..
    if dl_path.is_file():
        print(' [done]')
        return True
    else:
        print(' [done; but file not found]')
        return False


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nBye!')
