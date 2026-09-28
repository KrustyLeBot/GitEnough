import base64
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()


class GitError(Exception):
    pass


@dataclass
class Credential:
    username: str
    token: str


@dataclass
class RepoStatus:
    branch: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    changes: int = 0
    # Upstream configured but its remote branch no longer exists (git prints no ahead/behind line).
    upstream_gone: bool = False
    conflicts: int = 0  # unmerged paths


@dataclass
class Snapshot:
    kind: str  # "missing" | "notrepo" | "repo"
    status: RepoStatus | None = None
    branches: list[str] = field(default_factory=list)
    error: str = ""
    message: str = ""
    origin: str = ""  # URL of the local "origin" remote, "" when absent
    remote_mismatch: bool = False
    base: str = ""  # base branch of the repository ("" when none could be determined)
    on_origin: set = field(default_factory=set)  # branch names present on origin
    op: str = ""  # operation in progress: "rebase", "merge", "cherry-pick", "revert", "bisect"
    op_detail: str = ""  # e.g. "3/7" for a rebase
    op_branch: str = ""  # branch being rebased
    stashes: int = 0
    solutions: list = field(default_factory=list)  # .sln / .slnx paths relative to the repo
    base_ref: str = ""  # what the branch is compared with: origin/<base>, else <base>
    base_behind: int = 0  # commits of base_ref missing from the current branch (a rebase would bring them)


def _local_path(url: str) -> str | None:
    # Local repositories: "C:\x\repo", "C:/x/repo", "\\server\share\repo" and file:// URLs.
    url = url.strip()
    if url.lower().startswith("file://"):
        url = url[7:].lstrip("/") if re.match(r"^/*[A-Za-z]:", url[7:]) else url[7:]
    if re.match(r"^[A-Za-z]:[\\/]", url) or url.startswith(("\\\\", "/")):
        return url
    return None


def _repo_key(url: str) -> tuple[str, list[str]]:
    local = _local_path(url)
    if local is not None:
        parts = [s for s in re.split(r"[\\/]+", local) if s]
        if parts and parts[-1].lower().endswith(".git"):
            parts[-1] = parts[-1][:-4]
        return "", [s.lower() for s in parts]
    host, parts = parse_url(url)
    return host, [s.lower() for s in parts]


def same_repo(a: str, b: str) -> bool:
    # Same host and path, regardless of scheme (https/ssh), ".git" suffix or case.
    if _repo_key(a) == _repo_key(b):
        return True
    la, lb = _local_path(a), _local_path(b)
    if la and lb:
        # Same folder spelled differently (8.3 short names, case, junctions).
        try:
            return os.path.samefile(la, lb)
        except OSError:
            return False
    return False


def mask_url(url: str) -> str:
    # Old remotes may embed credentials (https://user:token@host/...).
    return re.sub(r"^(\w+://)[^/@]+@", r"\1***@", url)


def parse_url(url: str) -> tuple[str, list[str]]:
    url = url.strip()
    scp = re.match(r"^[\w.-]+@([\w.-]+):(.+)$", url)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        parsed = urlparse(url)
        host, path = parsed.hostname or "", parsed.path
    parts = [s for s in path.split("/") if s]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    return host.lower(), parts


def is_http(url: str) -> bool:
    return url.strip().lower().startswith(("http://", "https://"))


def folder_names(urls: list[str]) -> dict[str, str]:
    parsed = {u: parse_url(u)[1] for u in urls}
    counts = Counter(p[-1].lower() for p in parsed.values() if p)
    names = {}
    for url, parts in parsed.items():
        name = parts[-1] if parts else "repo"
        # Two repos with the same name from different groups: prefix with the group.
        if counts[name.lower()] > 1 and len(parts) > 1:
            name = f"{parts[-2]}-{name}"
        names[url] = re.sub(r'[<>:"/\\|?*]', "_", name)
    return names


def kill_all() -> None:
    with _running_lock:
        for proc in list(_running):
            try:
                proc.kill()
            except OSError:
                pass


def run_git(args: list[str], cwd: str | None = None, cred: Credential | None = None,
            timeout: int = 120, stdin: bytes | None = None, env_extra: dict | None = None) -> str:
    out = run_git_bytes(args, cwd, cred, timeout, stdin, env_extra)
    return out.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def run_git_bytes(args: list[str], cwd: str | None = None, cred: Credential | None = None,
                  timeout: int = 120, stdin: bytes | None = None, env_extra: dict | None = None) -> bytes:
    """Raw stdout: patches must keep the exact bytes (CRLF line endings, encodings)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Background threads must never trigger Git Credential Manager popups.
    env["GCM_INTERACTIVE"] = "never"
    # English messages, so explain() can recognise them whatever the user's locale.
    env["LC_ALL"] = "C"
    # Background reads must not rewrite .git/index (git status refreshes it otherwise): that write would
    # wake the file watcher, which runs git status again, in a loop. It also never competes with the
    # user's own git commands for index.lock.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.update(env_extra or {})
    if cred:
        basic = base64.b64encode(f"{cred.username}:{cred.token}".encode()).decode()
        # Passed through env, not argv, so the token is neither persisted nor visible in the process list.
        pairs = [("credential.helper", ""), ("http.extraHeader", f"Authorization: Basic {basic}")]
        env["GIT_CONFIG_COUNT"] = str(len(pairs))
        for i, (key, value) in enumerate(pairs):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
    try:
        proc = subprocess.Popen(
            ["git", *args], cwd=cwd, env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        # Python reports several Windows failures as "not found" (e.g. error 206, command line too long).
        if shutil.which("git") is None:
            raise GitError("git not found in PATH") from exc
        raise GitError(f"Could not start git: {exc}") from exc
    with _running_lock:
        _running.add(proc)
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise GitError(f"Timed out ({timeout} s)") from exc
    finally:
        with _running_lock:
            _running.discard(proc)
    if proc.returncode != 0:
        # Keep leading tabs: git indents the list of conflicting files with them.
        text = (err or out).decode("utf-8", errors="replace")
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]
        raise GitError("\n".join(lines[-40:]) or f"git {args[0]} failed (code {proc.returncode})")
    return out


def git_available() -> bool:
    try:
        run_git(["--version"], timeout=15)
        return True
    except GitError:
        return False


def is_repo(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


def git_dir(path: str) -> str:
    """The .git directory, following the "gitdir:" file used by worktrees and submodules."""
    dot = os.path.join(path, ".git")
    if os.path.isfile(dot):
        try:
            with open(dot, encoding="utf-8") as fh:
                target = fh.read().strip().removeprefix("gitdir:").strip()
            return target if os.path.isabs(target) else os.path.normpath(os.path.join(path, target))
        except OSError:
            pass
    return dot


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def in_progress(path: str) -> tuple[str, str, str]:
    """(operation, progress, branch) read from the .git directory, without spawning git."""
    gd = git_dir(path)
    for sub, step, total in (("rebase-merge", "msgnum", "end"), ("rebase-apply", "next", "last")):
        d = os.path.join(gd, sub)
        if os.path.isdir(d) and (sub == "rebase-merge" or not os.path.exists(os.path.join(d, "applying"))):
            done, end = _read(os.path.join(d, step)), _read(os.path.join(d, total))
            branch = _read(os.path.join(d, "head-name")).removeprefix("refs/heads/")
            return "rebase", f"{done}/{end}" if done and end else "", branch
        if os.path.isdir(d):
            return "am", "", ""
    for name, op in (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                     ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
        if os.path.exists(os.path.join(gd, name)):
            return op, "", ""
    return "", "", ""


def stash_count(path: str) -> int:
    # The stash reflog has one line per stash entry: no git process needed.
    text = _read(os.path.join(git_dir(path), "logs", "refs", "stash"))
    return len(text.splitlines()) if text else 0


_solutions_cache: dict[str, tuple[float, list[str]]] = {}


def find_solutions(path: str) -> list[str]:
    """.sln / .slnx files anywhere in the repository (tracked or new, never ignored ones)."""
    cached = _solutions_cache.get(path)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    try:
        out = run_git(["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                       "*.sln", "*.slnx"], cwd=path, timeout=30)
        found = sorted({f for f in out.split("\0") if f},
                       key=lambda f: (f.count("/"), f.lower()))  # Shallowest first.
    except GitError:
        found = []
    _solutions_cache[path] = (time.monotonic(), [f.replace("/", os.sep) for f in found])
    return _solutions_cache[path][1]


def abort_operation(url: str, path: str, op: str, cred: Credential | None) -> Snapshot:
    try:
        cmd = {"rebase": ["rebase", "--abort"], "merge": ["merge", "--abort"], "am": ["am", "--abort"],
               "cherry-pick": ["cherry-pick", "--abort"], "revert": ["revert", "--abort"],
               "bisect": ["bisect", "reset"]}[op]
        run_git(cmd, cwd=path, timeout=120)
        snap = inspect(url, path, cred, fetch=False)
        snap.message = f"{op.capitalize()} aborted"
        return snap
    except GitError as exc:
        snap = inspect(url, path, cred, fetch=False)
        snap.error = str(exc)
        return snap


def read_status(path: str) -> RepoStatus:
    out = run_git(["status", "--porcelain=v2", "--branch"], cwd=path, timeout=60)
    st = RepoStatus()
    has_ab = False
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
            st.branch = None if head == "(detached)" else head
        elif line.startswith("# branch.upstream "):
            st.upstream = line[len("# branch.upstream "):]
        elif line.startswith("# branch.ab "):
            ahead, behind = line[len("# branch.ab "):].split()
            st.ahead, st.behind = abs(int(ahead)), abs(int(behind))
            has_ab = True
        elif line and not line.startswith("#"):
            st.changes += 1
            if line.startswith("u "):
                st.conflicts += 1
    st.upstream_gone = bool(st.upstream) and not has_ab
    return st


# Base branch chosen per repository in its settings (folder id -> branch); set by the main window.
BASE_OVERRIDES: dict[str, str] = {}


def behind_count(path: str, ref: str) -> int:
    """Commits of ref that HEAD does not contain."""
    try:
        return int(run_git(["rev-list", "--count", f"HEAD..{ref}", "--"], cwd=path, timeout=30).strip() or 0)
    except (GitError, ValueError):
        return 0


# Candidate base branches in priority order; set from the config at startup.
BASE_CANDIDATES: list[str] = ["develop", "main", "master"]


def base_branch(path: str, branches: list[str] | None = None) -> str:
    names = set(branches if branches is not None else list_branches(path))
    for name in BASE_CANDIDATES:
        if name in names:
            return name
    # origin/HEAD is a small symbolic ref file: "ref: refs/remotes/origin/<default branch>".
    head = _read(os.path.join(common_dir(path), "refs", "remotes", "origin", "HEAD"))
    if head.startswith("ref: refs/remotes/origin/"):
        return head[len("ref: refs/remotes/origin/"):]
    try:
        ref = run_git(["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], cwd=path, timeout=15).strip()
    except GitError:
        return ""
    return ref.split("/", 1)[1] if "/" in ref else ""


def common_dir(path: str) -> str:
    """Directory holding refs and config (differs from git_dir for linked worktrees)."""
    gd = git_dir(path)
    shared = _read(os.path.join(gd, "commondir"))
    return os.path.normpath(os.path.join(gd, shared)) if shared else gd


def _refs_from_disk(path: str) -> list[str] | None:
    """Branch ref names read from packed-refs and loose ref files; None when git must be asked."""
    cd = common_dir(path)
    if os.path.exists(os.path.join(cd, "reftable")):
        return None  # reftable backend: only git can read it.
    refs = set()
    for line in _read(os.path.join(cd, "packed-refs")).splitlines():
        if line and line[0] not in "#^":
            name = line.split(" ", 1)[-1]
            if name.startswith(("refs/heads/", "refs/remotes/")):
                refs.add(name)
    for top in ("heads", "remotes"):
        base = os.path.join(cd, "refs", top)
        for folder, _dirs, files in os.walk(base):
            rel = os.path.relpath(folder, cd).replace(os.sep, "/")
            refs.update(f"{rel}/{f}" for f in files if not f.endswith(".lock"))
    return sorted(refs)


def list_refs(path: str) -> tuple[list[str], set[str]]:
    """(local and remote branch names merged, branch names that exist on origin)."""
    disk = _refs_from_disk(path)
    out = "\n".join(disk) if disk is not None else run_git(
        ["for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"], cwd=path, timeout=30)
    names, on_origin = set(), set()
    for ref in out.splitlines():
        if ref.startswith("refs/heads/"):
            names.add(ref[len("refs/heads/"):])
        elif ref.startswith("refs/remotes/"):
            remote, _, branch = ref[len("refs/remotes/"):].partition("/")
            if branch and branch != "HEAD":
                names.add(branch)
                if remote == "origin":
                    on_origin.add(branch)
    return sorted(names, key=str.lower), on_origin


def local_branches(path: str) -> list[str]:
    disk = _refs_from_disk(path)
    refs = disk if disk is not None else run_git(["for-each-ref", "--format=%(refname)", "refs/heads"],
                                                 cwd=path, timeout=30).split()
    return sorted((r[len("refs/heads/"):] for r in refs if r.startswith("refs/heads/")), key=str.lower)


def list_branches(path: str) -> list[str]:
    return list_refs(path)[0]


def origin_url(path: str) -> str:
    """Effective URL of origin, as git resolves it (insteadOf rewrites included)."""
    try:
        return run_git(["remote", "get-url", "origin"], cwd=path, timeout=15).strip()
    except GitError:
        return ""


CONFIG_URL_RE = re.compile(r'^\s*\[remote\s+"origin"\]\s*$')


def origin_url_fast(path: str) -> str:
    """origin URL read from .git/config; "" when absent (caller then asks git)."""
    in_origin = False
    for line in _read(os.path.join(common_dir(path), "config")).splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = bool(CONFIG_URL_RE.match(stripped))
        elif in_origin and "=" in stripped:
            key, _, value = stripped.partition("=")
            if key.strip().lower() == "url":
                return value.strip().strip('"')
    return ""


def inspect(url: str, path: str, cred: Credential | None, fetch: bool) -> Snapshot:
    if not os.path.isdir(path):
        return Snapshot("missing")
    if not is_repo(path):
        return Snapshot("notrepo", error="Folder exists but is not a git repository")
    origin = origin_url_fast(path)
    # The raw config value can differ from the effective URL (url.insteadOf): confirm with git.
    if url and (not origin or not same_repo(origin, url)):
        origin = origin_url(path)
    # No URL at all: a local-only repository found on disk, nothing to compare with.
    mismatch = bool(url) and (not origin or not same_repo(origin, url))
    error = ""
    # Never fetch a mismatched origin: it may be dead, or on a host the configured PAT must not reach.
    if fetch and not mismatch and origin:
        try:
            run_git(["fetch", "--prune"], cwd=path, cred=cred, timeout=180)
        except GitError as exc:
            error = f"Fetch failed: {exc}"
    try:
        branches, on_origin = list_refs(path)
        snap = Snapshot("repo", read_status(path), branches, error)
        snap.on_origin = on_origin
        snap.op, snap.op_detail, snap.op_branch = in_progress(path)
        snap.stashes = stash_count(path)
        snap.solutions = find_solutions(path)
        snap.base = BASE_OVERRIDES.get(os.path.normcase(os.path.normpath(path))) or base_branch(path, branches)
        if snap.base in branches:
            branches.insert(0, branches.pop(branches.index(snap.base)))
        st = snap.status
        if st and st.branch and snap.base and st.branch != snap.base and not in_progress(path)[0]:
            snap.base_ref = f"origin/{snap.base}" if snap.base in on_origin else snap.base
            snap.base_behind = behind_count(path, snap.base_ref)
    except GitError as exc:
        snap = Snapshot("repo", error=str(exc))
    snap.origin, snap.remote_mismatch = origin, mismatch
    return snap


AUTO_STASH = "GitEnough auto-stash"


def _stash_ref(path: str) -> str:
    try:
        return run_git(["rev-parse", "-q", "--verify", "refs/stash"], cwd=path, timeout=15).strip()
    except GitError:
        return ""


def _with_stash(path: str, action) -> None:
    """Stash local changes (untracked included), run action, then reapply them."""
    before = _stash_ref(path)
    run_git(["stash", "push", "--include-untracked", "-m", AUTO_STASH], cwd=path, timeout=120)
    # "No local changes to save" exits 0 without a stash; popping then would apply an unrelated stash.
    created = _stash_ref(path) != before
    try:
        action()
    except GitError:
        if created:
            run_git(["stash", "pop"], cwd=path, timeout=120)
        raise
    if created:
        try:
            run_git(["stash", "pop"], cwd=path, timeout=120)
        except GitError as exc:
            raise GitError("REAPPLY_CONFLICT\n" + str(exc)) from exc


def sync(url: str, path: str, cred: Credential | None, stash: bool = False) -> Snapshot:
    """Clone if missing, otherwise fetch and fast-forward when behind."""
    try:
        if not os.path.isdir(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            run_git(["clone", url, path], cred=cred, timeout=1800)
            snap = inspect(url, path, cred, fetch=False)
            snap.message = "Cloned"
            return snap
        if not is_repo(path):
            return Snapshot("notrepo", error="Folder exists but is not a git repository")
        snap = inspect(url, path, cred, fetch=False)
        if snap.remote_mismatch:
            snap.message = "Skipped: origin URL differs from the configured URL"
            return snap
        if snap.op:
            snap.message = f"Skipped: {snap.op} in progress"
            return snap
        if not snap.origin:
            snap.message = "Skipped: no origin remote"
            return snap
        run_git(["fetch", "--prune"], cwd=path, cred=cred, timeout=180)
        st = read_status(path)
        message = "Already up to date"
        if st.upstream and st.behind > 0:
            pull = lambda: run_git(["pull", "--ff-only"], cwd=path, cred=cred, timeout=600)  # noqa: E731
            _with_stash(path, pull) if stash else pull()
            message = f"Pulled {st.behind} commit(s)"
        snap = inspect(url, path, cred, fetch=False)
        snap.message = message
        return snap
    except GitError as exc:
        snap = inspect(url, path, cred, fetch=False)
        snap.error = str(exc)
        return snap


def switch(url: str, path: str, branch: str, cred: Credential | None, stash: bool = False) -> Snapshot:
    try:
        action = lambda: run_git(["switch", branch], cwd=path, timeout=120)  # noqa: E731
        _with_stash(path, action) if stash else action()
        snap = inspect(url, path, cred, fetch=False)
        snap.message = f"Switched to {branch}"
        return snap
    except GitError as exc:
        snap = inspect(url, path, cred, fetch=False)
        snap.error = str(exc)
        return snap


def push(url: str, path: str, cred: Credential | None) -> Snapshot:
    try:
        st = read_status(path)
        args = ["push"] if st.upstream and not st.upstream_gone else ["push", "-u", "origin", "HEAD"]
        run_git(args, cwd=path, cred=cred, timeout=600)
        snap = inspect(url, path, cred, fetch=False)
        snap.message = "Pushed"
        return snap
    except GitError as exc:
        snap = inspect(url, path, cred, fetch=False)
        snap.error = str(exc)
        return snap


def set_upstream(url: str, path: str, cred: Credential | None) -> Snapshot:
    """Make the current branch track origin/<same name>."""
    try:
        branch = read_status(path).branch
        if not branch:
            raise GitError("Detached HEAD: switch to a branch first")
        run_git(["branch", f"--set-upstream-to=origin/{branch}", branch], cwd=path, timeout=30)
        snap = inspect(url, path, cred, fetch=False)
        snap.message = f"{branch} now tracks origin/{branch}"
        return snap
    except GitError as exc:
        snap = inspect(url, path, cred, fetch=False)
        snap.error = str(exc)
        return snap


def web_url(url: str, branch: str | None = None) -> str | None:
    """Browser URL of a remote (GitHub, GitLab, Bitbucket, Azure DevOps, generic HTTPS)."""
    url = url.strip()
    if _local_path(url) is not None:
        return None
    host, parts = parse_url(url)
    if not host or not parts:
        return None
    if host == "ssh.dev.azure.com" and parts[0] == "v3" and len(parts) >= 4:
        host, parts = "dev.azure.com", [parts[1], parts[2], "_git", parts[3]]
    elif host.endswith("vs-ssh.visualstudio.com") and len(parts) >= 4 and parts[0] == "v3":
        host, parts = f"{parts[1]}.visualstudio.com", [parts[2], "_git", parts[3]]
    # Keep a non-default HTTPS port, drop credentials.
    port = ""
    if is_http(url):
        netloc = urlparse(url).netloc.rsplit("@", 1)[-1]
        if ":" in netloc:
            port = ":" + netloc.split(":", 1)[1]
    scheme = "http" if url.lower().startswith("http://") else "https"
    base = f"{scheme}://{host}{port}/" + "/".join(parts)
    if not branch:
        return base
    if "_git" in parts:
        return f"{base}?version=GB{branch}"
    if "gitlab" in host:
        return f"{base}/-/tree/{branch}"
    if "bitbucket" in host:
        return f"{base}/src/{branch}"
    return f"{base}/tree/{branch}"


def fix_remote(url: str, path: str, cred: Credential | None) -> Snapshot:
    """Point "origin" at the configured URL, then fetch from it."""
    try:
        verb = "set-url" if origin_url(path) else "add"
        run_git(["remote", verb, "origin", url], cwd=path, timeout=15)
        snap = inspect(url, path, cred, fetch=True)
        if not snap.error:
            snap.message = "Origin updated"
        return snap
    except GitError as exc:
        snap = inspect(url, path, cred, fetch=False)
        snap.error = str(exc)
        return snap


def test_access(url: str, cred: Credential | None) -> str:
    run_git(["ls-remote", "--heads", url], cred=cred, timeout=60)
    return "Access OK"
