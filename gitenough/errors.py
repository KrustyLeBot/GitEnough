"""Turns raw git error output into a short, actionable explanation."""

from dataclasses import dataclass, field

from .git_ops import AUTO_STASH


@dataclass
class Explained:
    title: str
    detail: str
    hint: str = ""
    files: list[str] = field(default_factory=list)
    can_stash: bool = False
    raw: str = ""


def _files(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.startswith("\t")]


def _conflicts(raw: str) -> list[str]:
    marker = "Merge conflict in "
    return [line.split(marker, 1)[1].strip() for line in raw.splitlines() if marker in line]


def explain(raw: str, action: str = "This operation") -> Explained:
    text = raw.lower()
    files = _files(raw)
    if raw.startswith("REAPPLY_CONFLICT"):
        return Explained(
            "Conflict while reapplying your changes",
            f"{action} succeeded, but your stashed changes conflict with the new files.",
            f"Conflicted files now contain conflict markers. Your changes are also kept in the stash "
            f"\"{AUTO_STASH}\" until you resolve them (git stash drop when done).",
            _conflicts(raw), raw=raw.split("\n", 1)[-1])
    if "would be overwritten" in text and "untracked" in text:
        return Explained(
            "Untracked files are in the way",
            f"{action} would create files that already exist locally as untracked files.",
            "Stash them and retry, or move them out of the repository.", files, True, raw)
    if "would be overwritten" in text or "commit your changes or stash them" in text:
        return Explained(
            "Your local changes conflict",
            f"{action} would modify files that you have edited and not committed.",
            "Stash & retry saves your edits, runs the operation, then reapplies them. "
            "Or commit your changes first.", files, True, raw)
    if "not possible to fast-forward" in text or "diverg" in text:
        return Explained(
            "Branches have diverged",
            "Your local branch and the remote branch both have new commits.",
            "Merge or rebase manually (in your IDE or with git pull --rebase), then refresh.", raw=raw)
    if "rejected" in text and ("fetch first" in text or "non-fast-forward" in text):
        return Explained(
            "Push rejected",
            "The remote branch has commits you do not have locally.",
            "Pull first, then push again.", raw=raw)
    if any(s in text for s in ("authentication failed", "could not read username", "terminal prompts disabled",
                               "returned error: 401", "returned error: 403", "permission denied")):
        return Explained(
            "Authentication failed",
            "The server refused the credentials.",
            "Add or update the PAT for this host in Settings > Access (PAT), and check its permissions.",
            raw=raw)
    if "repository not found" in text or "does not appear to be a git repository" in text:
        return Explained(
            "Repository not found",
            "The server does not know this repository, or your token cannot see it.",
            "Check the URL in Settings > Repositories and the PAT permissions.", raw=raw)
    if any(s in text for s in ("could not resolve host", "unable to access", "timed out", "connection refused")):
        return Explained("Network error", "The git server could not be reached.",
                         "Check your connection or VPN, then retry.", raw=raw)
    if "please tell me who you are" in text or "user.email" in text:
        return Explained(
            "Git identity not set",
            "Git needs your name and email to create a commit.",
            'Run: git config --global user.name "Your Name" and git config --global user.email you@example.com',
            raw=raw)
    if any(s in text for s in ("conflict", "unmerged", "needs merge", "resolve your current index")):
        return Explained("Unresolved conflicts", "The repository has files with unresolved conflicts.",
                         "Resolve them in your IDE, stage them, then retry.", files, raw=raw)
    if "no tracking information" in text or "no upstream" in text:
        return Explained("No upstream branch", "This branch does not track a remote branch.",
                         "Push it once to create the remote branch.", raw=raw)
    first = next((l.strip() for l in raw.splitlines() if l.strip()), "Unknown error")
    return Explained("Git error", first.removeprefix("fatal: ").removeprefix("error: "), raw=raw)
