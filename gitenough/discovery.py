"""Find every git repository under the root folder and match them with the configured URLs."""

import os
from dataclasses import dataclass

from .git_ops import folder_names, origin_url, origin_url_fast, same_repo

# Folders that never contain repositories worth listing, and are expensive to walk.
SKIP_DIRS = {"node_modules", "bin", "obj", "packages", "dist", "build", "out", "target", "venv", ".venv",
             "__pycache__", "$recycle.bin", "system volume information"}
MAX_DEPTH = 8


@dataclass
class Found:
    path: str
    url: str  # configured URL, else the repository's own origin, else ""
    configured: bool  # the URL comes from the configuration
    exists: bool


def scan_repos(root: str, max_depth: int = MAX_DEPTH) -> list[str]:
    """Repository folders under root. A repository's own content is not walked (no nested scan)."""
    found: list[str] = []

    def walk(folder: str, depth: int):
        if os.path.exists(os.path.join(folder, ".git")):
            found.append(folder)
            return
        if depth >= max_depth:
            return
        try:
            with os.scandir(folder) as entries:
                subdirs = [e.path for e in entries
                           if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
                           and e.name.lower() not in SKIP_DIRS]
        except OSError:
            return
        for sub in sorted(subdirs, key=str.lower):
            walk(sub, depth + 1)

    if os.path.isdir(root):
        walk(os.path.normpath(root), 0)
    return found


def discover(root: str, urls: list[str]) -> tuple[list[Found], list[str]]:
    """(projects, origin URLs found on disk that the configuration does not list yet)."""
    repos = scan_repos(root)
    result: list[Found] = []
    matched: set[str] = set()
    by_path: dict[str, Found] = {}
    for path in repos:
        # Reading .git/config is instant; git is only asked when that gives nothing.
        origin = origin_url_fast(path) or origin_url(path)
        configured = next((u for u in urls if origin and same_repo(u, origin)), None)
        if configured:
            matched.add(configured)
        found = Found(path, configured or origin, bool(configured), True)
        result.append(found)
        by_path[os.path.normcase(path)] = found
    names = folder_names(urls)
    for url in urls:
        if url in matched:
            continue
        path = os.path.normpath(os.path.join(root, names[url]))
        existing = by_path.get(os.path.normcase(path))
        if existing is not None:
            # The expected folder holds another remote: keep the configured URL so "Fix remote" is offered.
            existing.url, existing.configured = url, True
        else:
            result.append(Found(path, url, True, False))
    # Computed last: a folder re-assigned to a configured URL above must not add its old origin.
    new_urls: list[str] = []
    for f in result:
        if not f.configured and f.url and not any(same_repo(f.url, u) for u in new_urls):
            new_urls.append(f.url)
    return result, new_urls
