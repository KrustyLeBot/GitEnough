"""Changelists: named groups of unstaged files, to split one piece of work into several commits.

New changes land in Default. A file moved to a changelist stays there while it has unstaged changes; once it
is staged, committed or discarded it leaves its changelist. Kept per clone in .git/gitenough/changelists.json,
never shared.
"""

import json
import os

from . import git_ops

FILE = os.path.join("gitenough", "changelists.json")
DEFAULT = "Default"


def _store(path: str) -> str:
    return os.path.join(git_ops.git_dir(path), FILE)


def load(path: str) -> tuple[list[str], dict[str, str]]:
    """(changelist names in order, {file: changelist}); files of Default are not listed."""
    try:
        with open(_store(path), encoding="utf-8") as fh:
            data = json.load(fh)
        names = [n for n in data.get("lists", []) if isinstance(n, str) and n and n != DEFAULT]
        files = {f: n for f, n in data.get("files", {}).items() if n in names}
        return names, files
    except (OSError, ValueError, AttributeError):
        return [], {}


def save(path: str, names: list[str], files: dict[str, str]) -> None:
    store = _store(path)
    os.makedirs(os.path.dirname(store), exist_ok=True)
    tmp = store + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"lists": names, "files": {f: n for f, n in files.items() if n in names}}, fh, indent=1)
    os.replace(tmp, store)


def create(path: str, name: str) -> None:
    names, files = load(path)
    if name not in names and name != DEFAULT:
        names.append(name)
        save(path, names, files)


def rename(path: str, old: str, new: str) -> None:
    names, files = load(path)
    if old in names and new not in names and new != DEFAULT:
        names[names.index(old)] = new
        save(path, names, {f: (new if n == old else n) for f, n in files.items()})


def delete(path: str, name: str) -> None:
    """Removes a changelist; its files go back to Default."""
    names, files = load(path)
    if name in names:
        names.remove(name)
        save(path, names, {f: n for f, n in files.items() if n != name})


def move(path: str, paths: list[str], name: str) -> None:
    """Puts files into a changelist (Default: out of any changelist)."""
    names, files = load(path)
    for p in paths:
        if name == DEFAULT or name not in names:
            files.pop(p, None)
        else:
            files[p] = name
    save(path, names, files)


def prune(path: str, unstaged: set[str], keep: set[str] = frozenset()) -> None:
    """Files without unstaged changes any more leave their changelist (staged, committed, discarded).

    keep: files to leave alone although they are not listed (hidden never-commit changes).
    """
    names, files = load(path)
    left = {f: n for f, n in files.items() if f in unstaged or f in keep}
    if left != files:
        save(path, names, left)
