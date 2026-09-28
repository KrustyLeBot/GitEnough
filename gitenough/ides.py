"""Locate and launch Visual Studio and VS Code."""

import os
import shutil
import subprocess
from functools import lru_cache

from .git_ops import CREATE_NO_WINDOW

VSWHERE = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                       "Microsoft Visual Studio", "Installer", "vswhere.exe")


@lru_cache(maxsize=1)
def devenv_path() -> str | None:
    """devenv.exe of the most recent Visual Studio installed (prerelease channels included)."""
    if not os.path.isfile(VSWHERE):
        return None
    cmd = [VSWHERE, "-latest", "-prerelease", "-products", "*", "-property", "productPath", "-nologo"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                             creationflags=CREATE_NO_WINDOW).stdout.strip().splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out[0] if out and os.path.isfile(out[0]) else None


def open_solution(solution: str) -> bool:
    devenv = devenv_path()
    try:
        if devenv:
            subprocess.Popen([devenv, solution])
        else:
            os.startfile(solution)  # Whatever Windows associates with .sln / .slnx.
        return True
    except OSError:
        return False


@lru_cache(maxsize=1)
def vscode_path() -> str | None:
    candidates = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "Code.exe"),
                  os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft VS Code", "Code.exe")]
    for exe in candidates:
        if os.path.isfile(exe):
            return exe
    return shutil.which("code")


def open_vscode(path: str) -> bool:
    exe = vscode_path()
    if not exe:
        return False
    try:
        subprocess.Popen([exe, path], creationflags=CREATE_NO_WINDOW)
        return True
    except OSError:
        return False
