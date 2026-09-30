"""Cherry-pick commits onto another branch: switch there (local changes put aside), apply, stop on conflicts.

The operation state (branch to come back to, stash of the local changes, number of commits) lives in
.git/gitenough-cherry-pick.json, so the window can be closed and reopened while git waits on a conflict.
"""

import json
import os
import time
from dataclasses import dataclass, field

from .git_ops import GitError, git_dir, in_progress, run_git
from .rebase import NO_EDITOR
from .repo import FileChange, list_changes

STATE = "gitenough-cherry-pick.json"


@dataclass
class Picked:
    sha: str
    subject: str
    author: str


@dataclass
class Setup:
    current: str  # branch checked out now ("" when detached)
    commits: list[Picked]
    dirty: bool  # local changes that must be put aside to switch
    local: list[str]  # local branches
    remote: list[str]  # remote branches without a local one (origin/x)


@dataclass
class Progress:
    active: bool
    dest: str = ""
    step: str = ""  # "2/3"
    subject: str = ""  # commit being applied
    conflicts: list[FileChange] = field(default_factory=list)
    resolved: list[FileChange] = field(default_factory=list)
    empty: bool = False  # the commit brings nothing new on this branch (already there)


def _state_path(path: str) -> str:
    return os.path.join(git_dir(path), STATE)


def read_state(path: str) -> dict:
    try:
        with open(_state_path(path), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_state(path: str, state: dict) -> None:
    with open(_state_path(path), "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def _clear_state(path: str) -> None:
    try:
        os.remove(_state_path(path))
    except OSError:
        pass


def prepare(path: str, shas: list[str]) -> Setup:
    try:
        current = run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=path, timeout=15).strip()
    except GitError:
        current = ""
    # Oldest first, whatever order they were selected in. Dates tie (commits made in the same second,
    # rebased ones): the number of ancestors does not.
    lines = run_git(["log", "--no-walk=unsorted", "--format=%H%x1f%s%x1f%an", *shas], cwd=path,
                    timeout=30).strip().splitlines()
    commits = [Picked(*line.split("\x1f", 2)) for line in lines if line.count("\x1f") == 2]
    depth = {c.sha: int(run_git(["rev-list", "--count", c.sha], cwd=path, timeout=30).strip() or 0)
             for c in commits}
    commits.sort(key=lambda c: depth[c.sha])
    staged, unstaged = list_changes(path)
    refs = run_git(["for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"], cwd=path,
                   timeout=30).split()
    local = sorted(r[len("refs/heads/"):] for r in refs if r.startswith("refs/heads/"))
    remote = sorted(r[len("refs/remotes/"):] for r in refs
                    if r.startswith("refs/remotes/") and not r.endswith("/HEAD")
                    and r.split("/", 3)[-1] not in local)
    return Setup(current, commits, bool(staged or [f for f in unstaged if f.code != "?"]), local, remote)


def progress(path: str) -> Progress:
    op, _step, _branch = in_progress(path)
    state = read_state(path)
    if op != "cherry-pick":
        return Progress(False, state.get("dest", ""))
    try:
        subject = run_git(["log", "-1", "--format=%s", "CHERRY_PICK_HEAD"], cwd=path, timeout=15).strip()
    except GitError:
        subject = ""
    total = int(state.get("total") or 0)
    todo = os.path.join(git_dir(path), "sequencer", "todo")
    left = 0
    if os.path.exists(todo):
        with open(todo, encoding="utf-8", errors="replace") as fh:
            left = sum(1 for line in fh if line.strip() and not line.startswith("#"))
    step = f"{max(1, total - left + 1)}/{total}" if total else ""
    staged, unstaged = list_changes(path)
    conflicts = [f for f in unstaged if f.code == "U"]
    return Progress(True, state.get("dest", ""), step, subject, conflicts, staged,
                    empty=not conflicts and not staged)


def start(path: str, shas: list[str], dest: str, record_origin: bool) -> Progress:
    """Switch to dest (local changes stashed first), cherry-pick; returns the stopped state on conflicts."""
    setup = prepare(path, shas)
    state = {"back": setup.current, "dest": dest, "total": len(setup.commits), "stash": "", "started": time.time()}
    if setup.dirty and dest != setup.current:
        label = f"GitEnough: local changes put aside to cherry-pick onto {dest}"
        run_git(["stash", "push", "--include-untracked", "-m", label], cwd=path, timeout=120)
        state["stash"] = run_git(["rev-parse", "stash@{0}"], cwd=path, timeout=15).strip()
    _write_state(path, state)
    try:
        if dest != setup.current:
            if dest in setup.local:
                run_git(["switch", dest], cwd=path, timeout=120)
            else:  # a remote branch: its local counterpart, tracking it
                run_git(["switch", "-c", dest.split("/", 1)[1], "--track", dest], cwd=path, timeout=120)
                state["dest"] = dest.split("/", 1)[1]
                _write_state(path, state)
    except GitError:
        _restore_stash(path, state)  # nothing was picked: put everything back as it was
        _clear_state(path)
        raise
    args = ["cherry-pick", "--allow-empty-message"] + (["-x"] if record_origin else []) + \
        [c.sha for c in setup.commits]
    try:
        run_git(args, cwd=path, timeout=600, env_extra=NO_EDITOR)
    except GitError:
        current = progress(path)
        if current.active:
            return current  # stopped on a conflict (or an empty commit): the window takes over
        raise
    return progress(path)


def resume(path: str, action: str) -> Progress:
    """action: "continue", "skip" or "abort"."""
    try:
        run_git(["cherry-pick", f"--{action}"], cwd=path, timeout=600, env_extra=NO_EDITOR)
    except GitError:
        current = progress(path)
        if current.active and action != "abort":
            return current
        raise
    return progress(path)


def _restore_stash(path: str, state: dict) -> str:
    sha = state.get("stash")
    if not sha:
        return ""
    refs = run_git(["stash", "list", "--format=%gd %H"], cwd=path, timeout=30).splitlines()
    ref = next((line.split()[0] for line in refs if line.endswith(sha)), None)
    if ref is None:
        return "The stash of your local changes is gone (applied or dropped already)."
    try:
        run_git(["stash", "pop", "--index", ref], cwd=path, timeout=120)
    except GitError:
        try:
            run_git(["stash", "pop", ref], cwd=path, timeout=120)
        except GitError as exc:
            return f"Your local changes stay in the stash {ref}: {exc}"
    return ""


def finish(path: str, go_back: bool) -> str:
    """After the cherry-pick: back to the branch it started from, local changes restored. Returns a warning."""
    state = read_state(path)
    notes = []
    back = state.get("back", "")
    try:
        current = run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=path, timeout=15).strip()
    except GitError:
        current = ""
    if go_back and back and back != current:
        try:
            run_git(["switch", back], cwd=path, timeout=120)
        except GitError as exc:
            notes.append(f"Could not switch back to {back}: {exc}")
            go_back = False
    if go_back or back == current:
        warning = _restore_stash(path, state)
        if warning:
            notes.append(warning)
    elif state.get("stash"):
        notes.append(f"Your local changes of {back} wait in a stash: apply it once back on {back}.")
    _clear_state(path)
    return "\n".join(notes)
