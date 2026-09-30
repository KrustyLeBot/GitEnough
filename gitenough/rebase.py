"""Rebase --onto (plan, run, resolve conflicts, push with lease) and reset of a branch to a remote one."""

import os
import re
import time
from dataclasses import dataclass, field

from .git_ops import Credential, GitError, git_dir, in_progress, read_status, run_git
from .repo import Commit, FileChange, list_changes

# Rebase must never wait for an editor: keep the original messages when continuing.
NO_EDITOR = {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
LOG_FORMAT = "%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%D%x1f%s%x1e"


def _commits(path: str, rev_range: list[str], limit: int = 400) -> list[Commit]:
    out = run_git(["log", f"--format={LOG_FORMAT}", f"-n{limit}", *rev_range, "--"], cwd=path, timeout=60)
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if record:
            sha, parents, author, email, ts, refs, subject = record.split("\x1f", 6)
            commits.append(Commit(sha, parents.split(), author, email, int(ts or 0),
                                  [r.strip() for r in refs.split(",") if r.strip()], subject))
    return commits


def _rev(path: str, ref: str) -> str:
    try:
        return run_git(["rev-parse", "-q", "--verify", f"{ref}^{{commit}}"], cwd=path, timeout=15).strip()
    except GitError:
        return ""


@dataclass
class RebaseSetup:
    branch: str
    onto: str
    history: list[Commit]  # recent commits of the branch, newest first (to pick the old base from)
    merge_base: str  # default old base: where the branch left onto
    upstream: str = ""  # origin/<branch> if it exists
    remote_sha: str = ""  # value of the upstream now: the lease for the final push
    behind_upstream: int = 0  # remote commits the local branch does not have
    ahead_upstream: int = 0
    onto_behind: int = 0  # the local onto branch lags behind its own remote
    changes: int = 0
    warnings: list[str] = field(default_factory=list)


def prepare(path: str, branch: str, onto: str) -> RebaseSetup:
    """Everything the setup page shows: history to choose the old base, warnings, lease."""
    history = _commits(path, [branch], 300)
    try:
        mb = run_git(["merge-base", onto, branch], cwd=path, timeout=30).strip()
    except GitError:
        mb = ""
    setup = RebaseSetup(branch, onto, history, mb)
    upstream = f"origin/{branch}"
    setup.remote_sha = _rev(path, f"refs/remotes/{upstream}")
    if setup.remote_sha:
        setup.upstream = upstream
        counts = run_git(["rev-list", "--left-right", "--count", f"{branch}...{upstream}"], cwd=path,
                         timeout=30).split()
        setup.ahead_upstream, setup.behind_upstream = int(counts[0]), int(counts[1])
    if not onto.startswith("origin/") and _rev(path, f"refs/remotes/origin/{onto}"):
        behind = run_git(["rev-list", "--count", f"{onto}..origin/{onto}"], cwd=path, timeout=30).strip()
        setup.onto_behind = int(behind or 0)
    setup.changes = read_status(path).changes
    if setup.behind_upstream:
        setup.warnings.append(
            f"{branch} is {setup.behind_upstream} commit(s) behind {upstream}: someone pushed to it. Rebasing "
            "your local copy and force-pushing would drop their work. Pull (or reset to the remote) first.")
    if setup.onto_behind:
        setup.warnings.append(f"Your local {onto} is {setup.onto_behind} commit(s) behind origin/{onto}: "
                              f"rebase onto origin/{onto} to get the latest base.")
    if setup.changes:
        setup.warnings.append(f"{setup.changes} uncommitted change(s): they are stashed during the rebase "
                              "and reapplied after (autostash).")
    return setup


def plan(path: str, old_base: str, branch: str) -> list[Commit]:
    """Commits that will be replayed: old_base (excluded) .. branch."""
    return _commits(path, [f"{old_base}..{branch}"], 1000)


@dataclass
class Progress:
    active: bool
    step: str = ""  # "2/5"
    subject: str = ""  # commit being applied
    conflicts: list[FileChange] = field(default_factory=list)
    resolved: list[FileChange] = field(default_factory=list)  # staged while the rebase is stopped
    onto_label: str = ""


def progress(path: str) -> Progress:
    op, step, _branch = in_progress(path)
    if op != "rebase":
        return Progress(False)
    gd = git_dir(path)
    try:
        # REBASE_HEAD is the commit git stopped on.
        subject = run_git(["log", "-1", "--format=%s", "REBASE_HEAD"], cwd=path, timeout=15).strip()
    except GitError:
        subject = ""
    staged, unstaged = list_changes(path)
    conflicts = [f for f in unstaged if f.code == "U"]
    onto = ""
    onto_file = os.path.join(gd, "rebase-merge", "onto")
    if os.path.exists(onto_file):
        sha = open(onto_file, encoding="utf-8").read().strip()
        try:
            onto = run_git(["name-rev", "--name-only", "--no-undefined", sha], cwd=path, timeout=15).strip()
        except GitError:
            onto = sha[:8]
    return Progress(True, step, subject, conflicts, staged, onto)


def start(path: str, onto: str, old_base: str, branch: str, autostash: bool) -> Progress:
    """Run the rebase; returns the stopped state on conflicts (not an error)."""
    args = ["rebase", "--onto", onto, old_base, branch] + (["--autostash"] if autostash else [])
    try:
        run_git(args, cwd=path, timeout=600, env_extra=NO_EDITOR)
    except GitError:
        state = progress(path)
        if state.active:
            return state  # Stopped on a conflict: the window takes over.
        raise
    return progress(path)


def resume(path: str, action: str) -> Progress:
    """action: "continue", "skip" or "abort"."""
    try:
        run_git(["rebase", f"--{action}"], cwd=path, timeout=600, env_extra=NO_EDITOR)
    except GitError:
        state = progress(path)
        if state.active and action != "abort":
            return state
        raise
    return progress(path)


def _short_ref(name: str) -> str:
    return name.removeprefix("refs/heads/").removeprefix("refs/remotes/").removeprefix("remotes/")


def conflict_sides(path: str) -> tuple[str, str]:
    """Real names of the two sides of a conflict: (git's "ours", git's "theirs").

    The UI never says ours/theirs: it names branches. Git swaps the meaning during a rebase ("ours" is
    the branch rebased onto, "theirs" is your branch), which is exactly what confuses everyone.
    """
    gd = git_dir(path)
    op, _step, rebased = in_progress(path)
    current = read_status(path).branch or "HEAD"
    if op == "rebase":
        onto = ""
        for sub in ("rebase-merge", "rebase-apply"):
            f = os.path.join(gd, sub, "onto")
            if os.path.exists(f):
                sha = open(f, encoding="utf-8").read().strip()
                try:
                    onto = _short_ref(run_git(["name-rev", "--name-only", "--refs=refs/heads/*",
                                               "--refs=refs/remotes/*", sha], cwd=path, timeout=15).strip())
                except GitError:
                    onto = ""
                onto = onto if onto and onto != "undefined" and "~" not in onto else sha[:8]
        return onto or "base", rebased or "your branch"
    if op == "merge":
        msg = os.path.join(gd, "MERGE_MSG")
        other = ""
        if os.path.exists(msg):
            first = open(msg, encoding="utf-8", errors="replace").readline()
            m = re.search(r"Merge (?:remote-tracking )?branch '([^']+)'", first)
            other = m.group(1) if m else ""
        return current, other or "merged branch"
    if op in ("cherry-pick", "revert"):
        head = "CHERRY_PICK_HEAD" if op == "cherry-pick" else "REVERT_HEAD"
        try:
            subject = run_git(["log", "-1", "--format=%h %s", head], cwd=path, timeout=15).strip()
        except GitError:
            subject = op
        # The branch the picked commit comes from, when it is on one: a name people recognise.
        try:
            source = run_git(["name-rev", "--name-only", "--no-undefined", "--refs=refs/heads/*",
                              "--refs=refs/remotes/*", head], cwd=path, timeout=15).strip()
        except GitError:
            source = ""
        source = _short_ref(re.split(r"[~^]", source)[0]) if source else ""
        if source and source != current:
            return current, f"{source} · {subject.split(' ', 1)[0]}"
        return current, subject[:50]
    # No operation in progress but conflicts left: a stash that did not apply cleanly.
    return current, "stash"


def take_side(path: str, file: str, side: str) -> None:
    """side: "ours" / "theirs" in git's terms; callers label them with conflict_sides()."""
    run_git(["checkout", f"--{side}", "--", file], cwd=path, timeout=30)
    run_git(["add", "--", file], cwd=path, timeout=30)


def mark_resolved(path: str, file: str) -> None:
    run_git(["add", "--", file], cwd=path, timeout=30)


MARKER_RE = re.compile(r"^(<{7}|={7}|>{7}|\|{7})( |$)")


def has_markers(path: str, file: str) -> bool:
    try:
        with open(os.path.join(path, file), encoding="utf-8", errors="replace") as fh:
            return any(MARKER_RE.match(line) for line in fh)
    except OSError:
        return False


def push_with_lease(path: str, branch: str, expected: str, cred: Credential | None) -> str:
    """Force push that only succeeds if the remote branch is still at `expected` (captured before rebasing)."""
    lease = f"--force-with-lease=refs/heads/{branch}:{expected}" if expected else "--force-with-lease"
    run_git(["push", lease, "-u", "origin", f"{branch}:{branch}"], cwd=path, cred=cred, timeout=600)
    return run_git(["rev-parse", "--short", branch], cwd=path, timeout=15).strip()


# ---------- reset a local branch to a remote branch ----------

@dataclass
class ResetPreview:
    target: str  # origin/<name>
    local: str  # local branch that will be (re)created: <name>
    current: str | None  # branch checked out now
    lost_commits: list[Commit]  # commits of the local branch not in the target
    changes: int


def reset_preview(path: str, target: str) -> ResetPreview:
    name = target.split("/", 1)[1]
    st = read_status(path)
    lost = _commits(path, [f"{target}..{name}"], 200) if _rev(path, f"refs/heads/{name}") else []
    return ResetPreview(target, name, st.branch, lost, st.changes)


def reset_to_remote(path: str, target: str, stash: bool, backup: bool, clean: bool, delete_old: bool) -> str:
    """Make the local branch named like `target` an exact copy of it, and check it out.

    Returns a short report. Nothing is lost silently: changes go to a stash and unpushed commits to a
    backup branch when requested.
    """
    name = target.split("/", 1)[1]
    st = read_status(path)
    previous = st.branch
    report = []
    if st.changes and stash:
        label = time.strftime(f"GitEnough: before reset to {target} %Y-%m-%d %H:%M")
        run_git(["stash", "push", "--include-untracked", "-m", label], cwd=path, timeout=120)
        report.append("local changes stashed")
    if backup and _rev(path, f"refs/heads/{name}") and _commits(path, [f"{target}..{name}"], 1):
        backup_name = time.strftime(f"backup/{name}-%Y%m%d-%H%M%S")
        run_git(["branch", backup_name, name], cwd=path, timeout=30)
        report.append(f"old commits kept in {backup_name}")
    # -C recreates the branch at the target (even when it is the one checked out); --discard-changes
    # drops what is left in the working tree once the stash option has been offered.
    run_git(["switch", "--discard-changes", "-C", name, "--track", target], cwd=path, timeout=120)
    if clean:
        run_git(["clean", "-fd", "-q"], cwd=path, timeout=120)
        report.append("untracked files removed")
    if delete_old and previous and previous != name:
        run_git(["branch", "-D", previous], cwd=path, timeout=30)
        report.append(f"deleted {previous}")
    return f"{name} now matches {target}" + (f" ({', '.join(report)})" if report else "")
