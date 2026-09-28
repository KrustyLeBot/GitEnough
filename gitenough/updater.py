"""Update check against the public repository, download with integrity check, and in-place replacement."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass

from . import __version__

REPO_URL = "https://github.com/KrustyLeBot/GitEnough"
# Overridable for testing (a file:// URL works too).
VERSION_URL = os.environ.get("GITENOUGH_UPDATE_URL",
                             "https://raw.githubusercontent.com/KrustyLeBot/GitEnough/main/release/version.json")
USER_AGENT = f"GitEnough/{__version__}"


@dataclass
class Release:
    version: str
    url: str
    sha256: str
    size: int
    notes: str = ""


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts + [0] * (3 - len(parts)))


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


API_COMMITS = "https://api.github.com/repos/KrustyLeBot/GitEnough/commits?path=release/version.json&per_page=1"
RAW_AT = "https://raw.githubusercontent.com/KrustyLeBot/GitEnough/{sha}/release/{name}"


def _get_json(url: str, timeout: int):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _release(data: dict, url: str) -> Release:
    return Release(str(data["version"]), url, str(data.get("sha256", "")).lower(),
                   int(data.get("size", 0)), str(data.get("notes", "")))


def fetch_release(timeout: int = 15) -> Release:
    """The published release.

    raw.githubusercontent.com caches what "main" points to for minutes, so right after a release it can
    serve the old manifest (or an exe that does not match it). Asking the API for the commit of the latest
    release, then reading both files at that exact commit, is always consistent. The branch URL stays as a
    fallback (API rate limit: 60 requests per hour and IP).
    """
    if "GITENOUGH_UPDATE_URL" not in os.environ:
        try:
            sha = _get_json(API_COMMITS, timeout)[0]["sha"]
            data = _get_json(RAW_AT.format(sha=sha, name="version.json"), timeout)
            return _release(data, RAW_AT.format(sha=sha, name="GitEnough.exe"))
        except (OSError, ValueError, KeyError, IndexError):
            pass
    data = _get_json(VERSION_URL, timeout)
    return _release(data, str(data["url"]))


def check() -> Release | None:
    """The published release when it is newer than this build, else None."""
    release = fetch_release()
    return release if is_newer(release.version) else None


def download(release: Release, progress=None) -> str:
    """Download the new executable to a temp file and verify it; returns its path."""
    target = os.path.join(tempfile.gettempdir(), f"GitEnough-{release.version}.exe.download")
    req = urllib.request.Request(release.url, headers={"User-Agent": USER_AGENT})
    digest, done = hashlib.sha256(), 0
    with urllib.request.urlopen(req, timeout=60) as resp, open(target, "wb") as out:
        total = int(resp.headers.get("Content-Length") or release.size or 0)
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    # A truncated or tampered download must never replace a working executable.
    if release.size and done != release.size:
        os.remove(target)
        raise OSError(f"Download incomplete ({done} of {release.size} bytes)")
    if release.sha256 and digest.hexdigest() != release.sha256:
        os.remove(target)
        raise OSError("Downloaded file does not match the published SHA-256")
    return target


def running_exe() -> str | None:
    """Path of the running GitEnough.exe, or None when running from source."""
    return sys.executable if getattr(sys, "frozen", False) else None


def install(current_exe: str, new_file: str) -> str:
    """Put new_file in place of current_exe, which may be running.

    Windows refuses to overwrite a running executable but lets it be renamed: the old one becomes
    <name>.old (removed at the next start) and the new one takes its name.
    """
    old = current_exe + ".old"
    if os.path.exists(old):
        os.remove(old)
    os.replace(current_exe, old)
    try:
        os.replace(new_file, current_exe)
    except OSError:
        os.replace(old, current_exe)  # Put the working version back.
        raise
    return current_exe


def relaunch(exe: str) -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe], creationflags=flags, close_fds=True, cwd=os.path.dirname(exe))


def cleanup_previous(exe: str | None = None) -> bool:
    """Remove the executable left behind by the last self-update; True once nothing is left.

    Right after an update the previous process is still exiting and keeps the .old file locked for a few
    seconds, so callers retry until this returns True.
    """
    exe = exe or running_exe()
    old = exe + ".old" if exe else ""
    if not old or not os.path.exists(old):
        return True
    try:
        os.remove(old)
        return True
    except OSError:
        return False
