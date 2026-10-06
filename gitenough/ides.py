"""Locate and launch Visual Studio and VS Code."""

import json
import os
import shutil
import subprocess
from functools import lru_cache

from .git_ops import CREATE_NO_WINDOW

VSWHERE = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                       "Microsoft Visual Studio", "Installer", "vswhere.exe")


VS_LAUNCHER = os.path.join(os.environ.get("CommonProgramFiles(x86)", r"C:\Program Files (x86)\Common Files"),
                           "Microsoft Shared", "MSEnv", "VSLauncher.exe")
VS_LATEST, VS_SELECTOR, VS_WINDOWS = "", "selector", "windows"  # config values besides a devenv.exe path


@lru_cache(maxsize=1)
def visual_studios() -> list[tuple[str, str]]:
    """(label, devenv.exe) of every Visual Studio installed, newest first (prerelease channels included)."""
    if not os.path.isfile(VSWHERE):
        return []
    cmd = [VSWHERE, "-all", "-prerelease", "-products", "*", "-format", "json", "-utf8", "-nologo"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW).stdout
        items = json.loads(out.decode("utf-8") or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    found = []
    for item in items:
        exe = item.get("productPath") or ""
        if not os.path.isfile(exe):
            continue
        version = (item.get("catalog") or {}).get("productDisplayVersion") or item.get("installationVersion") or ""
        label = item.get("displayName") or os.path.basename(os.path.dirname(exe))
        # Two channels of one release share a display name: the version tells them apart.
        found.append((f"{label} ({version})" if version else label, exe, item.get("installationVersion") or ""))
    found.sort(key=lambda f: [int(x) for x in f[2].split(".") if x.isdigit()], reverse=True)
    return [(label, exe) for label, exe, _v in found]


def devenv_path(choice: str = VS_LATEST) -> str | None:
    """devenv.exe for the configured choice: a path when it is still installed, else the newest one."""
    installs = visual_studios()
    if choice not in (VS_LATEST, VS_SELECTOR, VS_WINDOWS) and os.path.isfile(choice):
        return choice
    stable = [exe for label, exe in installs if "insiders" not in label.lower() and "preview" not in label.lower()]
    return (stable or [exe for _l, exe in installs] or [None])[0]


def open_solution(solution: str, choice: str = VS_LATEST) -> bool:
    slnx = solution.lower().endswith(".slnx")
    if slnx and choice == VS_SELECTOR:
        choice = VS_LATEST  # the selector reads the version line of a .sln; a .slnx is XML without one
    try:
        if choice == VS_SELECTOR and os.path.isfile(VS_LAUNCHER):
            subprocess.Popen([VS_LAUNCHER, solution])  # picks the version the solution file asks for
            return True
        devenv = None if choice == VS_WINDOWS else devenv_path(choice)
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
