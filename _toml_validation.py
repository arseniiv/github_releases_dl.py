"""
Assert types of values parsed from TOML.
"""

from __future__ import annotations as _annotations
from datetime import date as _date, datetime as _datetime, time as _time
from types import NoneType as _NoneType, UnionType as _UnionType
from typing import Callable as _Callable, Final as _Final, Sequence as _Sequence, Union as _Union, overload as _overload


__all__ = ('toml_check', 'toml_check_get', 'toml_check_seq',)


type _TomlType = str | int | float | bool | _datetime | _date | _time | dict[str, object] | list[object] | None  # None just means absent value

_TOML_NAMES: _Final = {
    str: 'string',
    bool: 'bool',  # should come before `int`
    int: 'integer',  # should come before `float`
    float: 'float',
    _datetime: 'datetime',
    _date: 'date',
    _time: 'time',
    dict: 'table',
    list: 'array',
}

_UNION_TYPES: _Final = (type(_Union[str, int]), _UnionType)

def _toml_type_name[T: _TomlType](typ: type[T] | _UnionType) -> str:
    if isinstance(typ, _UNION_TYPES):
        names = list(map(_toml_type_name, typ.__args__))  # type: ignore
        return ' or '.join(names)
    for known_type, toml_type in _TOML_NAMES.items():
        if typ in (None, _NoneType):
            return 'absent'
        if issubclass(typ, known_type):
            return toml_type
    raise NotImplementedError(f'unknown type: {typ.__name__}')


@_overload
def toml_check(value: object, typ: type[dict], path: str) -> dict[str, object]: ...
@_overload
def toml_check(value: object, typ: type[list], path: str) -> list[object]: ...
@_overload
def toml_check[T: _TomlType](value: object, typ: type[T], path: str) -> T: ...
def toml_check[T: _TomlType](value: object, typ: type[T], path: str) -> T:
    """
    Ensure a parsed TOML value is of a given type. Raise otherwise.
    """
    if isinstance(value, typ):
        return value
    raise ValueError(f'`{path}` should be a {_toml_type_name(typ)}')


@_overload
def toml_check_get(data: dict[str, object], key: str, typ: type[dict], pre_path: str) -> dict[str, object]: ...
@_overload
def toml_check_get(data: dict[str, object], key: str, typ: type[list], pre_path: str) -> list[object]: ...
@_overload
def toml_check_get[T: _TomlType](data: dict[str, object], key: str, typ: type[T], pre_path: str) -> T: ...
def toml_check_get[T: _TomlType](data: dict[str, object], key: str, typ: type[T], pre_path: str) -> T:
    """
    Get a value from a dict and check it. Less repetition in paths.
    """
    path = f'{pre_path}.{key}' if pre_path else key
    return toml_check(data.get(key), typ, path)


@_overload
def toml_check_seq[T](seq: tuple[object, ...], checker: _Callable[[object], T]) -> tuple[T, ...]: ...
@_overload
def toml_check_seq[T](seq: list[object], checker: _Callable[[object], T]) -> list[T]: ...
def toml_check_seq[T](seq: _Sequence[object], checker: _Callable[[object], T]) -> _Sequence[T]:
    """
    Check the whole sequence and narrow its element type.
    Checker should raise on an incorrect type.
    This function isn't entirely correct for mutable sequences because they can be changed in another place and make this guaranteed type false.
    """
    for x in seq:
        checker(x)
    return seq # type: ignore


def _test() -> None:
    def raises(f: _Callable[[], object], text: str) -> bool:
        try:
            f()
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            return text in exc.args[0]
        return False

    assert toml_check(2, int, '') == 2
    assert toml_check(2, int | None, '') == 2
    assert toml_check(None, int | None, '') is None
    assert toml_check([34], list, '') == [34]
    assert raises(lambda: toml_check(2, str, 'x'), '`x` should be a string')
    assert raises(lambda: toml_check(dict(), list | None, 'y'), '`y` should be a array or absent')
    assert raises(lambda: toml_check((), list, 'x'), '`x` should be a array')
    assert raises(lambda: toml_check(None, bool, 'z'), '`z` should be a bool')

    assert toml_check_get({'a': None}, 'a', str | None, '') is None
    assert raises(lambda: toml_check_get({'a': None}, 'a', str, ''),
                   '`a` should be a string')
    assert raises(lambda: toml_check_get({'a': None}, 'a', dict, 'b'),
                   '`b.a` should be a table')

    test_seq = [1, 'ba', []]
    assert toml_check_seq(test_seq, lambda x: toml_check(x, list | str | int, '')) == test_seq
    assert raises(lambda: toml_check_seq(test_seq,
                                         lambda x: toml_check(x, str | None, 'c[]')),
                  '`c[]` should be a string or absent')

    print('all tests passed')


if __name__ == '__main__':
    _test()
