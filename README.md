# An interactive release scrapper for repositories on Github

Use Python 12+. Ensure `_toml_validation.py` is in the same folder.
Install pre-requisites:

```cmd
pip install -U PyGithub tomli-w
```

Place a `github_releases_dl.toml` in the same directory, with
configuration of what to do:

```toml
# query releases only up to a release with an older date: works faster
assume_releases_decreasing = true
# faulty (uses commitish without resolving) but you can use it if dates don't work
compare_commits = false
# your Github access token; if possible use a new one with no additional rights set
api_token = 'YOUR_GITHUB_ACCESS_TOKEN'
# path to all the downloads, either relative to the script's parent, or absolute
downloads_root = 'RELATIVE OR ABSOLUTE PATH'

# you can query releases granularly by dividing them into groups
# otherwise just define a single group
[group.GROUPNAME]        # beware the name is case-sensitive: probably do lowercase
# downloads get placed into this subfolder; groups can freely share folders
folder = 'RELATIVE PATH'

[[group.GROUPNAME.repos]]   # for each repo in this group
id = "USER/REPONAME"        # obviously, its id
# regex matchers: only releases matching these are shown
# normally it's assumed each should have just a single match
# if the list is empty or this key absent, every release is shown
# note: by default, `(?a)` mode is active, so `\d` is strictly `[0-9]` etc
matchers = [ 'REGEX1', 'REGEX2' ]
```

The script will create a file `github_releases_dl.cache.toml` to remember dates
of the releases you downloaded.

Don't forget you can specify several groups or release assets when asked, separated by spaces.
Inputting nothing will assume you want nothing. Inputting `*` will pick everything.

There's no download progress bar right now, sorry. But if any downloads were made,
corresponding folder windows will be opened for you.

## Manual vs automatic mode

When you run the script without arguments, it's executed in interactive mode
where you're asked how many releases to list, which one to pick and
which assets to download.

It can also run in automatic mode:

- `auto GROUP1 GROUP2...` — pick repos in groups `GROUP1`, `GROUP2`, ...
- `auto *` — pick everything.

In this mode, only the newest release (if there is any) is picked
and *every* matched asset is downloaded, even when a matcher has
multiple unplanned matches.
(Because some users are likely to not care and not specify any,
in which case it's fairly reasonable!)

If any `GROUPn` is invalid, the script in automatic mode halts
(useful to be sure no unintended action is done).
