"""The "Open in…" menu of a repository: terminals, IDEs, Explorer and the browser, in one button."""

import os
import shutil
import subprocess

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QToolButton

from . import git_ops, ides
from .widgets import icon


def powershell_path() -> str | None:
    """PowerShell 7 when installed, else Windows PowerShell."""
    return shutil.which("pwsh") or shutil.which("powershell")


def open_powershell(path: str) -> bool:
    exe = powershell_path()
    if not exe:
        return False
    subprocess.Popen([exe, "-NoExit", "-NoLogo"], cwd=path, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True


def fill(menu: QMenu, win, p) -> None:
    """Adds the targets available for this repository; unavailable ones are left out."""
    snap = p.snap
    on_disk = snap is not None and not p.missing and os.path.isdir(p.path)
    is_repo = on_disk and snap.kind == "repo"
    groups = []
    if on_disk:
        groups.append([("folder", "Explorer", lambda: win.open_path(p.path))])
    if is_repo:
        shells = [("terminal", "Git Bash", lambda: win.open_bash(p.path))]
        if powershell_path():
            shells.append(("powershell", "PowerShell", lambda: win.open_powershell(p.path)))
        groups.append(shells)
        editors = []
        sols = snap.solutions
        if len(sols) == 1:
            editors.append(("vs", f"Visual Studio  ·  {os.path.basename(sols[0])}",
                            lambda: win._launch_solution(p, sols[0])))
        elif sols:
            editors.append(("vs", sols, None))  # submenu, one entry per solution
        if ides.vscode_path():
            editors.append(("code", "VS Code", lambda: win.open_code(p.path)))
        groups.append(editors)
    if git_ops.web_url(p.url):
        web = [("globe", "Browser", lambda: win.open_web(p))]
        st = p.status
        if st and st.branch and snap and st.branch in snap.on_origin:
            web.append(("globe", f"Browser  ·  branch {st.branch}", lambda: win.open_web(p, branch=True)))
        groups.append(web)
    for group in (g for g in groups if g):
        if not menu.isEmpty():
            menu.addSeparator()
        for name, label, fn in group:
            if fn is None:
                sub = menu.addMenu(icon(name), f"Visual Studio  ·  {len(label)} solutions")
                for sol in label:
                    act = QAction(sol, sub)
                    act.triggered.connect(lambda _=False, sol=sol: win._launch_solution(p, sol))
                    sub.addAction(act)
                continue
            act = QAction(icon(name), label, menu)
            act.triggered.connect(fn)
            menu.addAction(act)


class OpenButton(QToolButton):
    """One button for every way of opening a repository; the menu is built when it opens."""

    def __init__(self, win, p, size: int = 18):
        super().__init__()
        self.win, self.p = win, p
        self.setIcon(icon("open", size))
        self.setIconSize(QSize(size, size))
        self.setToolTip("Open in… (Explorer, Git Bash, PowerShell, Visual Studio, VS Code, browser)")
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setMenu(QMenu(self))
        self.menu().aboutToShow.connect(self._fill)

    def _fill(self):
        self.menu().clear()
        fill(self.menu(), self.win, self.p)

    def available(self) -> bool:
        p = self.p
        return (p.snap is not None and not p.missing) or git_ops.web_url(p.url) is not None
