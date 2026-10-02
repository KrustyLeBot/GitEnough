"""Lines kept in the working folder that are never committed.

Each entry turns a block of lines of the clean file (`a`) into the lines really on disk (`b`), with a few
lines of context so it can be found again. Entries apply in list order on top of the clean file.

Reads (status, diffs) compute the clean file in memory. git commands that read or write the working
folder (add, stash, pull, switch, rebase, reset...) run with the clean file on disk, and the kept lines
are put back right after: they never reach the index, a stash or a commit, and survive a pull.
"""

import base64
import json
import os
import re
import threading
from contextlib import contextmanager

from . import git_ops
from .git_ops import GitError, run_git, run_git_bytes

CONTEXT = 3
FILE = os.path.join("gitenough", "never-commit.json")
MARKER = os.path.join("gitenough", "never-commit.aside")  # files written clean while a git command runs
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_lock = threading.Lock()
_repo_locks: dict[str, threading.RLock] = {}
_depth: dict[str, int] = {}
_cache: dict[str, tuple[float, list]] = {}  # key -> (mtime of the file, entries)
_recovered: set[str] = set()


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _store(path: str, name: str = FILE) -> str:
    return os.path.join(git_ops.git_dir(path), name)


def _text(line: bytes) -> str:
    return line.decode("utf-8", "surrogateescape")


def _bytes(line: str) -> bytes:
    return line.encode("utf-8", "surrogateescape")


# ---------- storage ----------

def entries(path: str) -> list[dict]:
    """[{file, a, b, line}], in application order. Cached until the file changes."""
    key = _key(path)
    if key not in _recovered:
        _recovered.add(key)
        _recover(path)
    store = _store(path)
    try:
        mtime = os.path.getmtime(store)
    except OSError:
        _cache.pop(key, None)
        return []
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(store, encoding="utf-8") as fh:
            items = [e for e in json.load(fh).get("entries", []) if e.get("file") and "a" in e and "b" in e]
    except (OSError, ValueError, AttributeError):
        items = []
    _cache[key] = (mtime, items)
    return items


def _save(path: str, items: list[dict]) -> None:
    store = _store(path)
    os.makedirs(os.path.dirname(store), exist_ok=True)
    tmp = store + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"entries": items}, fh, indent=1)
    os.replace(tmp, store)
    _cache.pop(_key(path), None)


def has_entries(path: str) -> bool:
    return os.path.exists(_store(path)) and bool(entries(path))


# ---------- line blocks ----------

def _split(data: bytes) -> list[list[bytes]]:
    """[[text, end of line]] of a file."""
    out = []
    for line in data.splitlines(keepends=True):
        text = line.rstrip(b"\r\n")
        out.append([text, line[len(text):]])
    return out


def _join(lines: list[list[bytes]]) -> bytes:
    return b"".join(t + e for t, e in lines)


def _find(lines: list[list[bytes]], block: list[bytes], hint: int) -> int:
    """Start of `block` in `lines` closest to `hint`, or -1."""
    if not block:
        return min(max(hint, 0), len(lines))
    n, best = len(block), -1
    for i in range(len(lines) - n + 1):
        if lines[i][0] == block[0] and all(lines[i + k][0] == block[k] for k in range(1, n)):
            if best < 0 or abs(i - hint) < abs(best - hint):
                best = i
    return best


def _replace(lines: list[list[bytes]], at: int, count: int, new: list[bytes]) -> None:
    ends = [e for _t, e in lines if e]
    eol = max(set(ends), key=ends.count) if ends else b"\n"  # the file's usual line ending
    at_end = at + count == len(lines) and count and lines[-1][1] == b""
    old_ends = [lines[at + k][1] for k in range(count)]
    repl = [[t, old_ends[k] if k < count and old_ends[k] else eol] for k, t in enumerate(new)]
    if at_end and repl:
        repl[-1][1] = b""  # the file had no newline at its end
    lines[at:at + count] = repl


def _reverse(data: bytes, items: list[dict]) -> tuple[bytes, list[dict]]:
    """The clean file, and the entries found in `data` (the others no longer match the file)."""
    lines, found = _split(data), []
    for e in reversed(items):
        b = [_bytes(t) for t in e["b"]]
        at = _find(lines, b, e.get("line", 0))
        if at < 0:
            continue
        _replace(lines, at, len(b), [_bytes(t) for t in e["a"]])
        found.append(e)
    found.reverse()
    return _join(lines), found


def _common_context(e: dict) -> tuple[int, int]:
    """Context lines before and after the change: the common prefix and suffix of a and b."""
    a, b = e["a"], e["b"]
    pre = 0
    while pre < min(len(a), len(b)) and a[pre] == b[pre]:
        pre += 1
    post = 0
    while post < min(len(a), len(b)) - pre and a[-1 - post] == b[-1 - post]:
        post += 1
    return pre, post


def _forward(data: bytes, items: list[dict]) -> tuple[bytes, list[dict], dict]:
    """`data` with the kept lines put back, the entries that could not be, and {id(entry): entry with the
    context of the new file} for those placed with less context (git changed lines around them)."""
    lines, failed, moved = _split(data), [], {}
    for e in items:
        pre, post = _common_context(e)
        placed = False
        for k in range(0, max(pre, post) + 1):  # like patch's fuzz: drop context lines until it fits
            cut_pre, cut_post = min(k, pre), min(k, post)
            a = e["a"][cut_pre:len(e["a"]) - cut_post]
            b = e["b"][cut_pre:len(e["b"]) - cut_post]
            if not a and (pre or post):
                break  # nothing left to anchor a pure insertion
            at = _find(lines, [_bytes(t) for t in a], e.get("line", 0) + cut_pre)
            if at < 0:
                continue
            _replace(lines, at, len(a), [_bytes(t) for t in b])
            if k:
                core_a = e["a"][pre:len(e["a"]) - post]
                core_b = e["b"][pre:len(e["b"]) - post]
                start = at + pre - cut_pre  # first changed line in the new file
                end = start + len(core_b)
                before = [_text(t) for t, _e in lines[max(start - CONTEXT, 0):start]]
                after = [_text(t) for t, _e in lines[end:end + CONTEXT]]
                moved[id(e)] = {**e, "a": before + core_a + after, "b": before + core_b + after,
                                "line": start - len(before)}
            placed = True
            break
        if not placed:
            failed.append(e)
    return _join(lines), failed, moved


def _read(path: str, file: str) -> bytes | None:
    try:
        with open(os.path.join(path, file), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write(path: str, file: str, data: bytes) -> None:
    with open(os.path.join(path, file), "wb") as fh:
        fh.write(data)


def clean(path: str, file: str) -> tuple[bytes | None, list[dict]]:
    """The file without its kept lines (None when unreadable), and the entries found in it."""
    data = _read(path, file)
    mine = [e for e in entries(path) if e["file"] == file]
    if data is None or not mine:
        return data, []
    return _reverse(data, mine)


def summary(path: str) -> tuple[int, int, int]:
    """(entries found in their file, files holding them, entries that no longer match)."""
    items = entries(path)
    found, files = 0, set()
    for file in {e["file"] for e in items}:
        n = len(clean(path, file)[1])
        if n:
            found += n
            files.add(file)
    return found, len(files), len(items) - found


def status(path: str) -> list[tuple[dict, bool]]:
    """Every entry with whether it is found in its file."""
    items, out = entries(path), []
    found_ids = set()
    for file in {e["file"] for e in items}:
        found_ids.update(id(e) for e in clean(path, file)[1])
    for e in items:
        out.append((e, id(e) in found_ids))
    return out


def reapply(path: str, chosen: list[dict]) -> int:
    """Writes lines that no longer match back into their file, where their context still fits; returns
    how many could not be placed."""
    left, updates = 0, {}
    for file in {e["file"] for e in chosen}:
        mine = [e for e in chosen if e["file"] == file]
        now = _read(path, file)
        if now is None:
            left += len(mine)
            continue
        data, failed, moved = _forward(now, mine)
        left += len(failed)
        if data != now:
            _write(path, file, data)
        updates.update({(e["file"], tuple(e["a"]), tuple(e["b"])): moved[id(e)] for e in mine if id(e) in moved})
    if updates:
        _save(path, [updates.get((e["file"], tuple(e["a"]), tuple(e["b"])), e) for e in entries(path)])
    return left


def forget(path: str, chosen: list[dict]) -> None:
    """Drop entries: their lines stay in the file and show up as regular changes again."""
    drop = [(e["file"], e["a"], e["b"]) for e in chosen]
    _save(path, [e for e in entries(path) if (e["file"], e["a"], e["b"]) not in drop])


# ---------- reads: status and diffs without the kept lines ----------

def _blob(path: str, file: str, data: bytes, write: bool = False) -> str:
    # --path applies the file's filters (line endings), so the hash compares with the index.
    args = ["hash-object", "--stdin", f"--path={file}"] + (["-w"] if write else [])
    return run_git(args, cwd=path, timeout=60, stdin=data).strip()


def hidden(path: str, candidates: list[tuple[str, str]]) -> set[str]:
    """Files among (file, index blob) whose only unstaged change is kept lines."""
    files = {e["file"] for e in entries(path)}
    out = set()
    for file, index_sha in candidates:
        if file not in files:
            continue
        data, found = clean(path, file)
        if data is not None and found:
            try:
                if _blob(path, file, data) == index_sha:
                    out.add(file)
            except GitError:
                pass
    return out


def clean_diff(path: str, file: str, ignore_ws: bool, context: int) -> str | None:
    """Unstaged diff of a file without its kept lines; None when it holds none."""
    data, found = clean(path, file)
    if data is None or not found:
        return None
    new = _blob(path, file, data, write=True)
    old = run_git(["rev-parse", f":{file}"], cwd=path, timeout=30).strip()
    args = ["diff", "--no-color", "--no-ext-diff", f"-U{context}"] + (["-w"] if ignore_ws else [])
    text = run_git([*args, old, new], cwd=path, timeout=60)
    head, sep, body = text.partition("\n@@")
    names = {f"a/{old}": f"a/{file}", f"b/{new}": f"b/{file}"}
    head = re.sub(r"[ab]/[0-9a-f]{40,64}", lambda m: names.get(m[0], m[0]), head)
    return head + sep + body


# ---------- writes: git commands run with the clean files on disk ----------

def _recover(path: str) -> None:
    """Puts back kept lines left aside by a git command that never finished (crash, kill)."""
    marker = _store(path, MARKER)
    try:
        with open(marker, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return
    _restore(path, {f: (base64.b64decode(v["orig"]), base64.b64decode(v["clean"]), v["entries"])
                    for f, v in saved.items()})
    os.remove(marker)


def _restore(path: str, saved: dict) -> None:
    updates = {}
    for file, (orig, clean_data, items) in saved.items():
        now = _read(path, file)
        if now is None:
            continue  # deleted by the command: its entries no longer match
        if now == clean_data:
            _write(path, file, orig)  # the command left the file alone: back to the exact bytes
            continue
        data, _failed, moved = _forward(now, items)  # the failed ones show as "no longer match"
        if data != now:
            _write(path, file, data)
        updates.update({(e["file"], tuple(e["a"]), tuple(e["b"])): moved[id(e)] for e in items if id(e) in moved})
    if updates:
        _save(path, [updates.get((e["file"], tuple(e["a"]), tuple(e["b"])), e) for e in entries(path)])


@contextmanager
def aside(path: str, files: list[str] | None = None):
    """Runs the block with the clean files on disk, then puts the kept lines back."""
    key = _key(path)
    with _lock:
        lock = _repo_locks.setdefault(key, threading.RLock())
    with lock:
        if _depth.get(key):
            yield
            return
        saved = {}
        targets = {e["file"] for e in entries(path)}
        for file in sorted(targets if files is None else targets & set(files)):
            data = _read(path, file)
            if data is None:
                continue
            mine = [e for e in entries(path) if e["file"] == file]
            clean_data, found = _reverse(data, mine)
            if found and clean_data != data:
                saved[file] = (data, clean_data, found)
        marker = _store(path, MARKER)
        if saved:
            with open(marker, "w", encoding="utf-8") as fh:
                json.dump({f: {"orig": base64.b64encode(o).decode(), "clean": base64.b64encode(c).decode(),
                               "entries": items} for f, (o, c, items) in saved.items()}, fh)
            for file, (_o, clean_data, _i) in saved.items():
                _write(path, file, clean_data)
        _depth[key] = 1
        try:
            yield
        finally:
            _depth[key] = 0
            if saved:
                _restore(path, saved)
                try:
                    os.remove(marker)
                except OSError:
                    pass


TOUCHES_WORKTREE = {"pull", "switch", "checkout", "merge", "rebase", "cherry-pick", "revert", "reset", "am", "rm",
                    "mv", "add", "stash", "apply", "update-index", "restore", "read-tree", "checkout-index",
                    "commit"}


def _touches_worktree(args: list[str]) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-c", "-C") else 1
    if i >= len(args) or args[i] not in TOUCHES_WORKTREE:
        return False
    sub, rest = args[i], args[i + 1:]
    if sub == "stash":
        return not rest or rest[0] not in ("list", "show", "drop", "store", "clear", "create")
    if sub == "apply":
        return "--cached" not in rest
    if sub == "restore":
        return "--worktree" in rest or "-W" in rest or not ("--staged" in rest or "-S" in rest)
    if sub == "commit":
        return any(a in ("-a", "--all", "-i", "--include", "-o", "--only") for a in rest)
    if sub == "update-index":
        return "--index-info" not in rest
    if sub == "read-tree":
        return "-u" in rest
    return True


def guard(cwd: str, args: list[str]):
    """Context for a git command, or None when it cannot meet kept lines."""
    if not _touches_worktree(args) or not has_entries(cwd):
        return None
    return aside(cwd)


def hidden_filter(path: str, candidates: list[tuple[str, str]]) -> set[str]:
    return hidden(path, candidates) if has_entries(path) else set()


git_ops.WORKTREE_GUARD = guard
git_ops.HIDDEN_FILTER = hidden_filter


# ---------- marking lines ----------

def add(path: str, file: str, full: bool, selection: dict, shape: list[int]) -> int:
    """Keeps the selected hunks / lines of the unstaged diff out of every commit; returns the entries added.

    selection maps a hunk index to None (whole hunk) or to the set of its line indexes, as in
    repo.apply_selection; shape is the number of body lines per hunk in the diff the user saw.
    """
    with aside(path, [file]):  # the diff below is then the one shown: without the lines kept before
        raw = run_git_bytes(["diff", "--no-color", "--no-ext-diff", f"-U{100000 if full else CONTEXT}",
                             "--", file], cwd=path, timeout=60)
    hunks = []
    for line in raw.split(b"\n"):
        if line.startswith(b"@@"):
            hunks.append([_text(line).rstrip("\r")])
        elif hunks:
            hunks[-1].append(_text(line.rstrip(b"\r")))
    if hunks and hunks[-1][-1] == "":
        hunks[-1].pop()
    if [len(h) - 1 for h in hunks] != shape:
        raise GitError("The file changed since the diff was displayed. Refresh and try again.")
    new_entries = []
    for idx, hunk in enumerate(hunks):
        if idx not in selection:
            continue
        chosen = selection[idx]
        m = HUNK_RE.match(hunk[0])
        start = int(m[2]) - 1 if m else 0
        seq = []  # (tag, text): " " in both, "-" only in the clean file, "+" only on disk
        for i, line in enumerate(hunk[1:]):
            tag, text = line[:1], line[1:]
            if tag == "\\":
                continue
            picked = chosen is None or i in chosen
            if tag == "+":
                seq.append(("+" if picked else " ", text))
            elif tag == "-":
                if picked:
                    seq.append(("-", text))
            else:
                seq.append((" ", text))
        changed = [k for k, (tag, _t) in enumerate(seq) if tag != " "]
        if not changed:
            continue
        lo, hi = max(changed[0] - CONTEXT, 0), min(changed[-1] + CONTEXT + 1, len(seq))
        line_at = start + sum(1 for tag, _t in seq[:lo] if tag != "-")
        seq = seq[lo:hi]
        new_entries.append({"file": file, "a": [t for tag, t in seq if tag != "+"],
                            "b": [t for tag, t in seq if tag != "-"], "line": line_at})
    if not new_entries:
        return 0
    # The new lines sit on the clean file of before, so they apply first.
    items = new_entries + entries(path)
    _save(path, items)
    data = _read(path, file)
    if data is not None:
        _clean, found = _reverse(data, [e for e in items if e["file"] == file])
        if not all(any(f is e for f in found) for e in new_entries):
            _save(path, items[len(new_entries):])
            raise GitError("These lines could not be told apart from the rest of the file. "
                           "Select a larger block and try again.")
    return len(new_entries)
