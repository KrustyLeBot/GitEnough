"""Working-tree and history operations used by the Changes and History windows."""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .git_ops import GitError, run_git, run_git_bytes

FULL_CONTEXT = 100000
MAX_UNTRACKED_BYTES = 2_000_000


@dataclass
class FileChange:
    path: str
    code: str  # M A D R C T U ?  (U = conflict, ? = untracked)
    staged: bool
    orig: str = ""  # source path of a rename or copy

    @property
    def key(self) -> tuple[bool, str]:
        return self.staged, self.path


@dataclass
class Commit:
    sha: str
    parents: list[str]
    author: str
    email: str
    time: int
    refs: list[str]
    subject: str


@dataclass
class History:
    commits: list[Commit] = field(default_factory=list)
    remotes: list[str] = field(default_factory=list)
    head: str = ""


def list_changes(path: str) -> tuple[list[FileChange], list[FileChange]]:
    out = run_git(["status", "--porcelain=v2", "-z", "--untracked-files=all"], cwd=path, timeout=60)
    entries = out.split("\0")
    staged, unstaged = [], []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        kind = entry[0]
        if kind == "?":
            unstaged.append(FileChange(entry[2:], "?", False))
        elif kind == "u":
            unstaged.append(FileChange(entry.split(" ", 10)[10], "U", False))
        elif kind in "12":
            fields = entry.split(" ", 8 if kind == "1" else 9)
            file, orig = fields[-1], ""
            if kind == "2":
                orig = entries[i]
                i += 1
            x, y = fields[1][0], fields[1][1]
            if x != ".":
                staged.append(FileChange(file, x, True, orig if x in "RC" else ""))
            if y != ".":
                unstaged.append(FileChange(file, y, False))
    key = lambda f: f.path.lower()  # noqa: E731
    return sorted(staged, key=key), sorted(unstaged, key=key)


def _untracked_as_diff(path: str, file: str) -> str:
    full = os.path.join(path, file)
    try:
        size = os.path.getsize(full)
        with open(full, "rb") as fh:
            data = fh.read(MAX_UNTRACKED_BYTES)
    except OSError as exc:
        raise GitError(f"Cannot read {file}: {exc}") from exc
    if b"\0" in data[:8000]:
        return "Binary files /dev/null and b/file differ\n"
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    header = "new file mode\n" + (f"truncated {size}\n" if size > MAX_UNTRACKED_BYTES else "")
    return header + f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{l}\n" for l in lines)


def change_diff(path: str, fc: FileChange, ignore_ws: bool = False, full: bool = False) -> str:
    if fc.code == "?":
        return _untracked_as_diff(path, fc.path)
    args = ["diff", "--no-color", "--no-ext-diff", "-M", f"-U{FULL_CONTEXT if full else 3}"]
    if ignore_ws:
        args.append("-w")
    if fc.staged:
        args.append("--cached")
    args += ["--", *([fc.orig, fc.path] if fc.orig else [fc.path])]
    return run_git(args, cwd=path, timeout=60)


DIFF_SPLIT_RE = re.compile(r"(?m)^(?=diff --git )")


def all_diffs(path: str, files: list[FileChange]) -> dict[tuple[bool, str], str]:
    """Diff text of every changed file, keyed like FileChange.key, in two git calls in total."""
    result: dict[tuple[bool, str], str] = {}
    for staged in (False, True):
        args = ["-c", "core.quotepath=off", "diff", "--no-color", "--no-ext-diff", "-M"]
        text = run_git(args + (["--cached"] if staged else []), cwd=path, timeout=120)
        for chunk in DIFF_SPLIT_RE.split(text):
            if not chunk.startswith("diff --git "):
                continue
            new = old = ""
            for line in chunk.split("\n", 12)[:12]:
                if line.startswith("+++ b/"):
                    new = line[6:]
                elif line.startswith("--- a/"):
                    old = line[6:]
            name = new or old or chunk.split("\n", 1)[0].rsplit(" b/", 1)[-1]
            result[(staged, name)] = chunk
    for fc in files:
        if fc.code == "?":
            try:
                result[fc.key] = _untracked_as_diff(path, fc.path)
            except GitError:
                pass
        elif fc.key not in result:
            # Unusual path (quoted by git): fall back to a per-file diff.
            try:
                result[fc.key] = change_diff(path, fc)
            except GitError:
                pass
    return result


def _run_with_paths(path: str, args: list[str], paths: list[str], timeout: int = 300) -> None:
    """Run a git command on many files without hitting the Windows command-line limit (~32k chars).

    Paths go through stdin (--pathspec-from-file), and --literal-pathspecs keeps names containing
    [ * ? from being read as patterns.
    """
    data = "\0".join(paths).encode("utf-8")
    run_git(["--literal-pathspecs", *args, "--pathspec-from-file=-", "--pathspec-file-nul"], cwd=path,
            timeout=timeout, stdin=data)


def _chunks(paths: list[str], limit: int = 20000):
    """Batches whose joined length stays well under the command-line limit (for commands without
    --pathspec-from-file, like git clean)."""
    batch, size = [], 0
    for item in paths:
        if batch and size + len(item) + 3 > limit:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += len(item) + 3
    if batch:
        yield batch


def stage(path: str, files: list[FileChange]) -> None:
    _run_with_paths(path, ["add", "-A"], sorted({f.path for f in files}))


def unstage(path: str, files: list[FileChange]) -> None:
    paths = sorted({p for f in files for p in (f.path, f.orig) if p})
    try:
        _run_with_paths(path, ["restore", "--staged"], paths)
    except GitError:
        # Repository without any commit yet: there is no HEAD to restore from.
        _run_with_paths(path, ["rm", "--cached", "-r", "-q"], paths)


def discard(path: str, files: list[FileChange]) -> None:
    untracked = [f.path for f in files if f.code == "?"]
    tracked = [f.path for f in files if f.code != "?"]
    if tracked:
        _run_with_paths(path, ["restore"], tracked)
    for batch in _chunks(untracked):
        run_git(["--literal-pathspecs", "clean", "-f", "-q", "--", *batch], cwd=path, timeout=300)


HUNK_BYTES_RE = re.compile(rb"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _split_raw(raw: bytes) -> tuple[list[bytes], list[list[bytes]]]:
    header, hunks = [], []
    for line in raw.split(b"\n"):
        if line.startswith(b"@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            header.append(line)
    if hunks and hunks[-1][-1] == b"":
        hunks[-1].pop()
    return header, hunks


def _build_patch(header: list[bytes], hunks: list[list[bytes]], selection: dict, reverse: bool) -> bytes:
    """Keep the selected hunks/lines; neutralise the others so the patch still applies.

    Forward (stage): an unselected "+" is dropped, an unselected "-" becomes context.
    Reverse (unstage, discard): an unselected "+" becomes context, an unselected "-" is dropped.
    """
    out = list(header)
    for idx, hunk in enumerate(hunks):
        if idx not in selection:
            continue
        chosen = selection[idx]
        m = HUNK_BYTES_RE.match(hunk[0])
        old_start, new_start = int(m[1]), int(m[2])
        body, old_n, new_n, dropped = [], 0, 0, False
        for i, line in enumerate(hunk[1:]):
            tag = line[:1]
            if tag == b"\\":
                if not dropped:
                    body.append(line)
                continue
            dropped = False
            picked = chosen is None or i in chosen
            if tag == b"+":
                if picked:
                    body.append(line)
                    new_n += 1
                elif reverse:
                    body.append(b" " + line[1:])
                    old_n += 1
                    new_n += 1
                else:
                    dropped = True
            elif tag == b"-":
                if picked:
                    body.append(line)
                    old_n += 1
                elif not reverse:
                    body.append(b" " + line[1:])
                    old_n += 1
                    new_n += 1
                else:
                    dropped = True
            else:
                body.append(line)
                old_n += 1
                new_n += 1
        if any(l[:1] in (b"+", b"-") for l in body):
            out.append(b"@@ -%d,%d +%d,%d @@" % (old_start, old_n, new_start, new_n))
            out.extend(body)
    return b"\n".join(out) + b"\n"


def apply_selection(path: str, fc: FileChange, full: bool, selection: dict, action: str,
                    shape: list[int]) -> None:
    """Stage, unstage or discard some hunks / lines of one file.

    selection maps a hunk index to None (whole hunk) or to the set of its line indexes.
    shape is the number of body lines per hunk in the diff the user saw.
    """
    args = ["diff", "--no-color", "--no-ext-diff", f"-U{FULL_CONTEXT if full else 3}"]
    if fc.staged:
        args.append("--cached")
    # Raw bytes, so CRLF line endings survive into the patch.
    raw = run_git_bytes([*args, "--", fc.path], cwd=path, timeout=60)
    header, hunks = _split_raw(raw)
    if [len(h) - 1 for h in hunks] != shape:
        raise GitError("The file changed since the diff was displayed. Refresh and try again.")
    reverse = action != "stage"
    patch = _build_patch(header, hunks, selection, reverse)
    cmd = ["apply", "--recount", "--whitespace=nowarn"]
    if action != "discard":
        cmd.append("--cached")
    if reverse:
        cmd.append("--reverse")
    run_git([*cmd, "-"], cwd=path, timeout=60, stdin=patch)


def discard_all(path: str) -> None:
    run_git(["reset", "--hard", "-q"], cwd=path, timeout=120)
    run_git(["clean", "-fd", "-q"], cwd=path, timeout=120)


def stash_all(path: str) -> None:
    label = time.strftime("GitEnough: changes put aside %Y-%m-%d %H:%M")
    run_git(["stash", "push", "--include-untracked", "-m", label], cwd=path, timeout=120)


# ---------- stashes ----------

@dataclass
class Stash:
    ref: str  # stash@{n}: shifts when entries are dropped, so always re-checked against sha
    message: str
    time: int
    sha: str


def stashes(path: str) -> list[Stash]:
    out = run_git(["stash", "list", "--format=%gd%x1f%gs%x1f%ct%x1f%H"], cwd=path, timeout=30)
    result = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            result.append(Stash(parts[0], parts[1], int(parts[2] or 0), parts[3]))
    return result


def _parse_name_status(out: str, untracked: bool = False) -> list[FileChange]:
    parts, files, i = out.split("\0"), [], 0
    while i < len(parts):
        status = parts[i]
        i += 1
        if not status:
            continue
        code = status[0]
        if code in "RC":
            orig, file = parts[i], parts[i + 1]
            i += 2
        else:
            orig, file = "", parts[i]
            i += 1
        files.append(FileChange(file, "?" if untracked else code, True, orig))
    return files


def stash_files(path: str, st: Stash) -> list[FileChange]:
    base = ["diff-tree", "-r", "-M", "-z", "--name-status", "--no-commit-id"]
    files = _parse_name_status(run_git([*base, f"{st.sha}^1", st.sha], cwd=path, timeout=60))
    # With --include-untracked, the untracked files live in a third parent commit.
    parents = run_git(["log", "-1", "--format=%P", st.sha], cwd=path, timeout=15).split()
    if len(parents) >= 3:
        files += _parse_name_status(run_git([*base, "--root", parents[2]], cwd=path, timeout=60), untracked=True)
    return sorted(files, key=lambda f: f.path.lower())


def stash_diff(path: str, st: Stash, fc: FileChange, ignore_ws: bool = False, full: bool = False) -> str:
    opts = ["--no-color", "--no-ext-diff", "-M", f"-U{FULL_CONTEXT if full else 3}"] + (["-w"] if ignore_ws else [])
    if fc.code == "?":
        return run_git(["show", "--format=", *opts, f"{st.sha}^3", "--", fc.path], cwd=path, timeout=60)
    paths = [fc.orig, fc.path] if fc.orig else [fc.path]
    return run_git(["diff", *opts, f"{st.sha}^1", st.sha, "--", *paths], cwd=path, timeout=60)


def _check_stash(path: str, st: Stash) -> None:
    try:
        current = run_git(["rev-parse", st.ref], cwd=path, timeout=15).strip()
    except GitError:
        current = ""
    if current != st.sha:
        raise GitError("The stash list changed since it was displayed. Refresh and try again.")


def stash_apply(path: str, st: Stash, pop: bool) -> None:
    _check_stash(path, st)
    verb = "pop" if pop else "apply"
    try:
        # --index also restores what was staged; git refuses it when the index cannot be rebuilt.
        run_git(["stash", verb, "--index", st.ref], cwd=path, timeout=120)
    except GitError as exc:
        text = str(exc).lower()
        if "without --index" not in text and "conflicts in index" not in text:
            raise
        run_git(["stash", verb, st.ref], cwd=path, timeout=120)


def stash_drop(path: str, st: Stash) -> None:
    _check_stash(path, st)
    run_git(["stash", "drop", st.ref], cwd=path, timeout=60)


def stash_push(path: str, message: str, untracked: bool) -> None:
    args = ["stash", "push"] + (["--include-untracked"] if untracked else [])
    run_git(args + (["-m", message] if message else []), cwd=path, timeout=120)


# ---------- branches ----------

@dataclass
class Branch:
    name: str
    remote: bool = False
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    gone: bool = False
    time: int = 0
    subject: str = ""
    current: bool = False
    merged: bool = False  # fully merged into the base branch
    has_local: bool = False  # remote branch that already has a local counterpart


TRACK_RE = re.compile(r"(ahead|behind) (\d+)")


def list_branch_info(path: str, base: str) -> tuple[list[Branch], list[Branch]]:
    fmt = "%(refname)%1f%(upstream:short)%1f%(upstream:track)%1f%(committerdate:unix)%1f%(subject)%1f%(HEAD)"
    out = run_git(["for-each-ref", f"--format={fmt}", "refs/heads", "refs/remotes/origin"], cwd=path, timeout=30)
    local, remote = [], []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 6:
            continue
        ref, upstream, track, ts, subject, head = parts
        b = Branch("", upstream=upstream, time=int(ts or 0), subject=subject, current=head == "*",
                   gone="gone" in track)
        for kind, n in TRACK_RE.findall(track):
            setattr(b, kind, int(n))
        if ref.startswith("refs/heads/"):
            b.name = ref[len("refs/heads/"):]
            local.append(b)
        elif ref.startswith("refs/remotes/origin/") and not ref.endswith("/HEAD"):
            b.name, b.remote = ref[len("refs/remotes/"):], True
            remote.append(b)
    names = {b.name for b in local}
    target = f"origin/{base}" if any(r.name == f"origin/{base}" for r in remote) else base
    if base and (target in names or target.startswith("origin/")):
        merged = run_git(["branch", "--merged", target, "--format=%(refname:short)"], cwd=path, timeout=30)
        merged_set = set(merged.split())
        for b in local:
            b.merged = b.name in merged_set and b.name != base
    for r in remote:
        r.has_local = r.name.split("/", 1)[1] in names
    key = lambda b: (not b.current, b.name != base, b.name.lower())  # noqa: E731
    return sorted(local, key=key), sorted(remote, key=lambda b: b.name.lower())


def check_branch_name(path: str, name: str) -> str:
    try:
        return run_git(["check-ref-format", "--branch", name], cwd=path, timeout=15).strip()
    except GitError as exc:
        raise GitError(f"\"{name}\" is not a valid branch name") from exc


def create_branch(path: str, name: str, start: str) -> None:
    check_branch_name(path, name)
    # --no-track: a new feature branch must not track its start point (e.g. origin/develop).
    run_git(["branch", "--no-track", name, start], cwd=path, timeout=30)


def rename_branch(path: str, old: str, new: str) -> None:
    check_branch_name(path, new)
    run_git(["branch", "-m", old, new], cwd=path, timeout=30)


def delete_branches(path: str, names: list[str], force: bool) -> None:
    run_git(["branch", "-D" if force else "-d", *names], cwd=path, timeout=60)


def delete_remote_branch(path: str, name: str, cred) -> None:
    run_git(["push", "origin", "--delete", name], cwd=path, cred=cred, timeout=180)


def commit(path: str, message: str) -> str:
    run_git(["commit", "-m", message], cwd=path, timeout=120)
    return run_git(["rev-parse", "--short", "HEAD"], cwd=path, timeout=15).strip()


def history(path: str, scope: str, limit: int, base: str = "") -> History:
    """scope: "base" (current branch, its upstream and the base branch), "current" or "all"."""
    fmt = "%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%D%x1f%s%x1e"
    args = ["log", f"--format={fmt}", "--topo-order", f"-n{limit}"]
    if scope == "all":
        args += ["--exclude=refs/stash", "--all"]
    else:
        refs = ["HEAD", "@{upstream}"]
        if scope == "base" and base:
            refs += [base, f"origin/{base}"]
        # --ignore-missing skips refs that do not exist: one git process instead of one check per ref.
        args += ["--ignore-missing", *dict.fromkeys(refs), "--"]
    # Each git process costs ~80 ms on Windows: list the remotes while the log runs.
    with ThreadPoolExecutor(max_workers=1) as pool:
        remotes_job = pool.submit(run_git, ["remote"], path, None, 15)
        try:
            out = run_git(args, cwd=path, timeout=120)
        except GitError as exc:
            if "does not have any commits" in str(exc) or "bad default revision" in str(exc):
                return History()
            raise
        remotes = remotes_job.result().split()
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, author, email, ts, refs, subject = record.split("\x1f", 6)
        commits.append(Commit(sha, parents.split(), author, email, int(ts or 0),
                              [r.strip() for r in refs.split(",") if r.strip()], subject))
    # HEAD is in the decorations ("HEAD -> main", or "HEAD" when detached): no extra rev-parse.
    head = next((c.sha for c in commits if any(r == "HEAD" or r.startswith("HEAD -> ") for r in c.refs)), "")
    return History(commits, remotes, head)


def commit_body(path: str, sha: str) -> str:
    return run_git(["show", "-s", "--format=%B", sha], cwd=path, timeout=30).strip()


def commit_files(path: str, c: Commit) -> list[FileChange]:
    base = ["diff-tree", "-r", "-M", "-z", "--name-status", "--no-commit-id"]
    args = base + ([c.parents[0], c.sha] if c.parents else ["--root", c.sha])
    parts = run_git(args, cwd=path, timeout=60).split("\0")
    files, i = [], 0
    while i < len(parts):
        status = parts[i]
        i += 1
        if not status:
            continue
        code = status[0]
        if code in "RC":
            orig, file = parts[i], parts[i + 1]
            i += 2
        else:
            orig, file = "", parts[i]
            i += 1
        files.append(FileChange(file, code, True, orig))
    return files


def commit_diff(path: str, c: Commit, fc: FileChange, ignore_ws: bool = False, full: bool = False) -> str:
    opts = ["--no-color", "--no-ext-diff", "-M", f"-U{FULL_CONTEXT if full else 3}"] + (["-w"] if ignore_ws else [])
    paths = ["--", *([fc.orig, fc.path] if fc.orig else [fc.path])]
    if c.parents:
        return run_git(["diff", *opts, c.parents[0], c.sha, *paths], cwd=path, timeout=60)
    return run_git(["show", "--format=", *opts, c.sha, *paths], cwd=path, timeout=60)


# ---------- compare with base ----------

@dataclass
class Comparison:
    target: str  # ref compared against, e.g. origin/develop
    merge_base: str
    commits: list[Commit] = field(default_factory=list)  # commits on HEAD that target lacks
    files: list[FileChange] = field(default_factory=list)
    worktree: bool = False  # uncommitted changes included


def compare(path: str, target: str, include_worktree: bool) -> Comparison:
    """What the current branch changes compared with target, from their merge base (like a merge request)."""
    base = run_git(["merge-base", target, "HEAD"], cwd=path, timeout=30).strip()
    fmt = "%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%D%x1f%s%x1e"
    out = run_git(["log", f"--format={fmt}", f"{target}..HEAD"], cwd=path, timeout=60)
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if record:
            sha, parents, author, email, ts, refs, subject = record.split("\x1f", 6)
            commits.append(Commit(sha, parents.split(), author, email, int(ts or 0),
                                  [r.strip() for r in refs.split(",") if r.strip()], subject))
    args = ["diff", "--name-status", "-z", "-M", base] + ([] if include_worktree else ["HEAD"])
    files = _parse_name_status(run_git(args, cwd=path, timeout=60))
    return Comparison(target, base, commits, sorted(files, key=lambda f: f.path.lower()), include_worktree)


def compare_diff(path: str, cmp: Comparison, fc: FileChange, ignore_ws: bool = False, full: bool = False) -> str:
    opts = ["--no-color", "--no-ext-diff", "-M", f"-U{FULL_CONTEXT if full else 3}"] + (["-w"] if ignore_ws else [])
    revs = [cmp.merge_base] + ([] if cmp.worktree else ["HEAD"])
    paths = [fc.orig, fc.path] if fc.orig else [fc.path]
    return run_git(["diff", *opts, *revs, "--", *paths], cwd=path, timeout=60)


# ---------- file history & blame ----------

def file_history(path: str, file: str, limit: int = 500) -> list[tuple[Commit, str]]:
    """Commits touching file (renames followed), each with the file's path in that commit."""
    fmt = "%x1e%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%D%x1f%s"
    out = run_git(["log", "--follow", "-M", f"-n{limit}", f"--format={fmt}", "--name-only", "-z", "--", file],
                  cwd=path, timeout=120)
    result = []
    for record in out.split("\x1e"):
        record = record.strip("\n\0")
        if not record:
            continue
        head, _, names = record.partition("\n")
        sha, parents, author, email, ts, refs, subject = head.split("\x1f", 6)
        name = next((n for n in names.replace("\n", "\0").split("\0") if n.strip()), file).strip()
        result.append((Commit(sha, parents.split(), author, email, int(ts or 0),
                              [r.strip() for r in refs.split(",") if r.strip()], subject), name))
    return result


def file_commit_diff(path: str, c: Commit, file: str, ignore_ws: bool = False, full: bool = False) -> str:
    opts = ["--no-color", "--no-ext-diff", "-M", f"-U{FULL_CONTEXT if full else 3}"] + (["-w"] if ignore_ws else [])
    # --follow on a single commit: a rename shows its real changes instead of a whole new file.
    return run_git(["log", "-1", "-p", "--follow", "--format=", *opts, c.sha, "--", file], cwd=path, timeout=60)


@dataclass(slots=True)
class BlameLine:
    sha: str
    author: str
    time: int
    summary: str
    text: str
    first: bool  # first line of a run of lines from the same commit


def blame(path: str, file: str, rev: str = "") -> list[BlameLine]:
    out = run_git(["blame", "--porcelain", "-w", *([rev] if rev else []), "--", file], cwd=path, timeout=120)
    meta: dict[str, dict] = {}
    lines: list[BlameLine] = []
    current = None
    for line in out.split("\n"):
        if not line:
            continue
        if line.startswith("\t"):
            info = meta[current]
            prev = lines[-1].sha if lines else None
            lines.append(BlameLine(current, info.get("author", ""), info.get("time", 0), info.get("summary", ""),
                                   line[1:], current != prev))
            continue
        parts = line.split(" ")
        if len(parts[0]) == 40 and all(c in "0123456789abcdef" for c in parts[0]):
            current = parts[0]
            meta.setdefault(current, {})
        elif current:
            key, _, value = line.partition(" ")
            if key == "author":
                meta[current]["author"] = value
            elif key == "author-time":
                meta[current]["time"] = int(value or 0)
            elif key == "summary":
                meta[current]["summary"] = value
    return lines


# ---------- tags ----------

@dataclass
class Tag:
    name: str
    sha: str  # commit the tag points at
    subject: str
    time: int
    annotated: bool
    message: str = ""
    on_origin: bool | None = None  # None until checked against the remote


def list_tags(path: str) -> list[Tag]:
    fmt = "%(refname:short)%1f%(objecttype)%1f%(*objectname)%1f%(objectname)%1f%(contents:subject)%1f" \
          "%(creatordate:unix)%1f%(*subject)%1f%(subject)"
    out = run_git(["for-each-ref", "--sort=-creatordate", f"--format={fmt}", "refs/tags"], cwd=path, timeout=30)
    tags = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 8:
            continue
        name, kind, peeled, obj, message, ts, peeled_subject, subject = parts
        annotated = kind == "tag"
        tags.append(Tag(name, peeled if annotated else obj, peeled_subject if annotated else subject,
                        int(ts or 0), annotated, message if annotated else ""))
    return tags


def remote_tags(path: str, cred) -> set[str]:
    out = run_git(["ls-remote", "--tags", "--refs", "origin"], cwd=path, cred=cred, timeout=120)
    return {line.split("refs/tags/", 1)[1] for line in out.splitlines() if "refs/tags/" in line}


def create_tag(path: str, name: str, target: str, message: str) -> None:
    run_git(["check-ref-format", f"refs/tags/{name}"], cwd=path, timeout=15)
    args = ["tag", "-a", name, "-m", message] if message else ["tag", name]
    run_git([*args, target], cwd=path, timeout=30)


def push_tags(path: str, names: list[str], cred) -> None:
    run_git(["push", "origin", *[f"refs/tags/{n}" for n in names]], cwd=path, cred=cred, timeout=300)


def delete_tag(path: str, name: str) -> None:
    run_git(["tag", "-d", name], cwd=path, timeout=30)


def delete_remote_tag(path: str, name: str, cred) -> None:
    run_git(["push", "origin", "--delete", f"refs/tags/{name}"], cwd=path, cred=cred, timeout=180)


# ---------- ignore ----------

GLOB_SPECIAL = re.compile(r"([\[\]*?!#\\])")


def ignore_pattern(rel: str, is_dir: bool = False) -> str:
    """Root-anchored .gitignore pattern matching exactly this path."""
    escaped = GLOB_SPECIAL.sub(r"\\\1", rel.replace("\\", "/"))
    if escaped.endswith(" "):
        escaped = escaped[:-1] + "\\ "  # Trailing spaces are dropped unless escaped.
    return "/" + escaped + ("/" if is_dir else "")


def add_ignore(path: str, pattern: str, local_only: bool, untrack: list[str]) -> str:
    """Append pattern to .gitignore (or .git/info/exclude); returns the file written."""
    from .git_ops import common_dir
    target = os.path.join(common_dir(path), "info", "exclude") if local_only else os.path.join(path, ".gitignore")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    existing = ""
    if os.path.exists(target):
        with open(target, "rb") as fh:
            existing = fh.read().decode("utf-8", errors="replace")
    if pattern in (l.strip() for l in existing.splitlines()):
        return target  # Already there.
    newline = "\r\n" if "\r\n" in existing else "\n"
    with open(target, "ab") as fh:
        prefix = "" if not existing or existing.endswith(("\n", "\r")) else newline
        fh.write((prefix + pattern + newline).encode("utf-8"))
    if untrack:
        _run_with_paths(path, ["rm", "--cached", "-r", "-q"], untrack)
    return target
