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


def fetch_release(timeout: int = 15) -> Release:
    req = urllib.request.Request(VERSION_URL, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return Release(str(data["version"]), str(data["url"]), str(data.get("sha256", "")).lower(),
                   int(data.get("size", 0)), str(data.get("notes", "")))


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


def cleanup_previous() -> None:
    """Remove the executable left behind by the last self-update."""
    exe = running_exe()
    if exe and os.path.exists(exe + ".old"):
        try:
            os.remove(exe + ".old")
        except OSError:
            pass  # Still locked by the exiting process: next start.
