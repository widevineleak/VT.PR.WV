import itertools
from typing import Iterable, Sequence


class CaseInsensitiveDict(dict):
    """
    A dictionary with case-insensitive keys.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for k in self.keys():
            if not isinstance(k, (str, bytes)):
                raise ValueError(f"dictionary keys must be str or bytes, not {type(k)}")

    def _resolve_key(self, key):
        if not isinstance(key, (str, bytes)):
            raise ValueError(f"dictionary keys must be str or bytes, not {type(key)}")
        return next((x for x in self.keys() if x.casefold() == key.casefold()), key)

    def __contains__(self, key):
        return super().__contains__(self._resolve_key(key))

    def __getitem__(self, key):
        return super().__getitem__(self._resolve_key(key))

    def __setitem__(self, key, value):
        return super().__setitem__(self._resolve_key(key), value)

    def get(self, key, default=None):
        return super().get(self._resolve_key(key), default)

    def pop(self, key):
        return super().pop(self._resolve_key(key))

    def popitem(self, key):
        return super().popitem(self._resolve_key(key))

    def setdefault(self, key, value=None):
        return super().setdefault(self._resolve_key(key), value)

    def update(self, other):
        for k, v in other.items():
            if isinstance(v, dict):
                v = CaseInsensitiveDict(v)
            self[k] = v


class ObserverDict(dict):
    """
    A simple observer interface for dicts that accepts a callback to trigger when it's changed.
    """

    def on_change(self):
        pass

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.on_change()

    def clear(self):
        super().clear()
        self.on_change()

    def update(self, obj):
        for k, v in obj.items():
            self[k] = v


def as_lists(*args):
    """Convert any input objects to list objects."""
    for item in args:
        yield item if isinstance(item, list) else [item]


def as_list(*args):
    """
    Convert any input objects to a single merged list object.

    Example:
    >>> as_list('foo', ['buzz', 'bizz'], 'bazz', 'bozz', ['bar'], ['bur'])
    ['foo', 'buzz', 'bizz', 'bazz', 'bozz', 'bar', 'bur']
    """
    if args == (None,):
        return []
    return list(itertools.chain.from_iterable(as_lists(*args)))


def first(iterable):
    return next(iter(iterable))


def first_or_else(iterable, default):
    item = next(iter(iterable or []), None)
    if item is None:
        return default
    return item


def first_or_none(iterable):
    return first_or_else(iterable, None)


def flatten(items, ignore_types=str):
    """
    Flatten items recursively.

    Example:
    >>> list(flatten(["foo", [["bar", ["buzz", [""]], "bee"]]]))
    ['foo', 'bar', 'buzz', '', 'bee']
    >>> list(flatten("foo"))
    ['foo']
    >>> list(flatten({1}, set))
    [{1}]
    """
    if isinstance(items, (Iterable, Sequence)) and not isinstance(items, ignore_types):
        for i in items:
            yield from flatten(i, ignore_types)
    else:
        yield items


def merge_dict(*dicts):
    """Recursively merge dicts into dest in-place."""
    dest = dicts[0]
    for d in dicts[1:]:
        for key, value in d.items():
            if isinstance(value, dict):
                node = dest.setdefault(key, {})
                merge_dict(node, value)
            else:
                dest[key] = value
