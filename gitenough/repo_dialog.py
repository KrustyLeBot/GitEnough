"""Per-repository settings (remote URL, display name, base branch) and deletion."""

import ctypes
import os
from ctypes import wintypes

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout)

from . import git_ops, repo
from .git_ops import GitError, parse_url, same_repo
from .style import C


# ---------- recycle bin ----------

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR)]


FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400


def move_to_recycle_bin(path: str) -> None:
    """Send a folder to the Windows Recycle Bin (restorable), raising OSError on failure."""
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"  # The API wants a double-NUL-terminated list.
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted or os.path.exists(path):
        raise OSError(f"Could not move {path} to the Recycle Bin (code {result}). Is a file open in another app?")


def risks(path: str) -> list[str]:
    """Work that exists only in this folder and would be lost with it."""
    out = []
    try:
        st = git_ops.read_status(path)
        if st.changes:
            out.append(f"{st.changes} uncommitted change(s)")
    except GitError:
        pass
    n = git_ops.stash_count(path)
    if n:
        out.append(f"{n} stash(es)")
    try:
        local, _remote = repo.list_branch_info(path, "")
        unpushed = [b.name for b in local if b.ahead or not b.upstream or b.gone]
        if unpushed:
            out.append(f"{len(unpushed)} branch(es) with commits not on origin: {', '.join(unpushed[:6])}"
                       + ("…" if len(unpushed) > 6 else ""))
    except GitError:
        pass
    return out


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    return label


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(True)
    return label


class RepoSettingsDialog(QDialog):
    """Everything about one repository: remote URL, name, base branch, list options, removal."""

    def __init__(self, main, project):
        super().__init__(main)
        self.main, self.p = main, project
        self.action = ""  # "hide" or "delete" when the user chose one
        cfg = main.config
        ov = cfg.repo_overrides.get(project.id, {})
        self.setWindowTitle(f"{project.title or project.name} · settings")
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(8)

        title = QLabel(project.title or project.name)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        lay.addWidget(title)
        where = QHBoxLayout()
        path = QLabel(project.path)
        path.setObjectName("muted")
        path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_btn = QPushButton("Open folder")
        open_btn.setObjectName("rowAction")
        open_btn.clicked.connect(lambda: main.open_path(project.path))
        where.addWidget(path, 1)
        where.addWidget(open_btn)
        lay.addLayout(where)

        # --- remote
        lay.addSpacing(8)
        lay.addWidget(_section("Remote URL (origin)"))
        self.url = QLineEdit(project.snap.origin if project.snap and project.snap.origin else project.url)
        self.url.setPlaceholderText("https://gitlab.example.com/group/project.git")
        lay.addWidget(self.url)
        row = QHBoxLayout()
        self.test_btn = QPushButton("Test access")
        self.test_btn.setObjectName("rowAction")
        self.test_btn.clicked.connect(self.test)
        self.apply_url = QPushButton("Change URL")
        self.apply_url.setObjectName("rowAction")
        self.apply_url.setToolTip("git remote set-url origin <URL>, then fetch; the repository list is updated too")
        self.apply_url.clicked.connect(self.change_url)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        row.addWidget(self.test_btn)
        row.addWidget(self.apply_url)
        row.addWidget(self.result, 1)
        lay.addLayout(row)
        others = [r for r in self._remotes() if not r.startswith("origin\t")]
        if others:
            lay.addWidget(_hint("Other remotes: " + "  ·  ".join(others)))

        # --- display & behaviour
        lay.addSpacing(8)
        lay.addWidget(_section("In GitEnough"))
        grid = QHBoxLayout()
        grid.addWidget(QLabel("Display name"))
        self.title = QLineEdit(ov.get("title", ""))
        self.title.setPlaceholderText(project.name)
        grid.addWidget(self.title, 1)
        grid.addSpacing(12)
        grid.addWidget(QLabel("Base branch"))
        self.base = QComboBox()
        self.base.setEditable(True)
        auto = f"(automatic: {project.snap.base})" if project.snap and project.snap.base else "(automatic)"
        self.base.addItem(auto)
        self.base.addItems(project.snap.branches if project.snap else [])
        self.base.setCurrentText(ov.get("base", "") or auto)
        self.base.setMinimumWidth(180)
        grid.addWidget(self.base)
        lay.addLayout(grid)
        self.include = QCheckBox("Include in Pull all")
        self.include.setChecked(main.is_enabled(project))
        self.pinned = QCheckBox("Pin to the top of the list")
        self.pinned.setChecked(project.id in cfg.pinned)
        opts = QHBoxLayout()
        opts.addWidget(self.include)
        opts.addWidget(self.pinned)
        opts.addStretch()
        lay.addLayout(opts)

        # --- branch tools
        lay.addSpacing(8)
        lay.addWidget(_section("Branch tools"))
        tools = QHBoxLayout()
        is_repo = bool(project.snap and project.snap.kind == "repo")
        rebase_btn = QPushButton("Rebase onto…")
        rebase_btn.setToolTip("Move your commits onto another base (git rebase --onto), resolve conflicts, "
                              "force push with lease")
        rebase_btn.clicked.connect(lambda: self._finish("rebase"))
        reset_btn = QPushButton("Reset to remote branch…")
        reset_btn.setToolTip("Recreate the local branch from origin/<branch>: for when the remote was rewritten")
        reset_btn.clicked.connect(lambda: self._finish("reset"))
        for b in (rebase_btn, reset_btn):
            b.setEnabled(is_repo)
            tools.addWidget(b)
        tools.addStretch()
        lay.addLayout(tools)

        # --- danger zone
        lay.addSpacing(10)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {C['border']};")
        lay.addWidget(line)
        danger = QHBoxLayout()
        hide = QPushButton("Hide from GitEnough")
        hide.setToolTip("Keep the folder, stop listing it (Settings > Repositories shows it again)")
        hide.clicked.connect(lambda: self._finish("hide"))
        delete = QPushButton("Delete from disk…")
        delete.setObjectName("danger")
        delete.setToolTip("Move the whole folder to the Recycle Bin")
        delete.clicked.connect(lambda: self._finish("delete"))
        delete.setEnabled(os.path.isdir(project.path))
        danger.addWidget(hide)
        danger.addWidget(delete)
        danger.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        danger.addWidget(cancel)
        danger.addWidget(save)
        lay.addLayout(danger)

    def _remotes(self) -> list[str]:
        if not (self.p.snap and self.p.snap.kind == "repo"):
            return []
        try:
            out = git_ops.run_git(["remote", "-v"], cwd=self.p.path, timeout=15)
        except GitError:
            return []
        return sorted({git_ops.mask_url(l.rsplit(" ", 1)[0]) for l in out.splitlines() if l.strip()})

    def _show(self, ok: bool | None, text: str):
        self.result.setStyleSheet(f"color: {C['muted'] if ok is None else C['green'] if ok else C['red']};")
        self.result.setText(text)

    def test(self):
        url = self.url.text().strip()
        if not url:
            return
        self._show(None, "Testing…")
        host = parse_url(url)[0]
        cred = self.main.creds.get(host) if git_ops.is_http(url) else None

        def done(result, err):
            try:
                self._show(not err, result if not err else str(err).splitlines()[-1])
            except RuntimeError:
                pass  # Dialog closed meanwhile.

        self.main.tasks.submit_network(git_ops.test_access, done, url, cred)

    def change_url(self):
        url = self.url.text().strip()
        if not url or not parse_url(url)[1]:
            self._show(False, "Not a valid git URL")
            return
        if self.p.snap and self.p.snap.origin and same_repo(url, self.p.snap.origin) and url == self.p.snap.origin:
            self._show(None, "Unchanged")
            return
        self.main.change_remote_url(self.p, url)
        self._show(True, "Updated: fetching from the new URL…")

    def _finish(self, action: str):
        self.action = action
        self.accept()

    def apply(self):
        """Save the display / list options into the config (the remote URL is applied immediately)."""
        cfg = self.main.config
        ov = dict(cfg.repo_overrides.get(self.p.id, {}))
        title = self.title.text().strip()
        base = self.base.currentText().strip()
        ov["title"] = title
        ov["base"] = "" if base.startswith("(automatic") else base
        ov = {k: v for k, v in ov.items() if v}
        if ov:
            cfg.repo_overrides[self.p.id] = ov
        else:
            cfg.repo_overrides.pop(self.p.id, None)
        if self.include.isChecked() != self.main.is_enabled(self.p):
            self.main.set_enabled(self.p, self.include.isChecked())
        cfg.pinned = [x for x in cfg.pinned if x != self.p.id] + ([self.p.id] if self.pinned.isChecked() else [])
        cfg.save()


class ResetDialog(QDialog):
    """Make a local branch an exact copy of a remote branch (typically after a rebase done by someone else)."""

    def __init__(self, main, project):
        super().__init__(main)
        self.main, self.p = main, project
        snap = project.snap
        current = snap.status.branch if snap and snap.status else None
        self.setWindowTitle("Reset to a remote branch")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(8)
        title = QLabel("Reset to a remote branch")
        title.setStyleSheet("font-size: 13pt; font-weight: 700;")
        lay.addWidget(title)
        lay.addWidget(_hint("The local branch with the same name is recreated from the remote one and checked "
                            "out. Use it when the remote branch was rewritten (rebased, force-pushed) and you just "
                            "want the clean remote version."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Remote branch"))
        self.target = QComboBox()
        targets = [f"origin/{b}" for b in sorted(snap.on_origin, key=str.lower)] if snap else []
        self.target.addItems(targets)
        if current and current in (snap.on_origin if snap else set()):
            self.target.setCurrentText(f"origin/{current}")
        elif snap and snap.base in snap.on_origin:
            self.target.setCurrentText(f"origin/{snap.base}")
        self.target.currentTextChanged.connect(self.update_preview)
        row.addWidget(self.target, 1)
        lay.addLayout(row)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        lay.addWidget(self.info)
        self.fetch = QCheckBox("Fetch first, to reset to the latest remote state")
        self.fetch.setChecked(True)
        self.stash = QCheckBox("Stash my uncommitted changes (otherwise they are lost)")
        self.stash.setChecked(True)
        self.backup = QCheckBox("Keep the current local commits in a backup/… branch")
        self.backup.setChecked(True)
        self.clean = QCheckBox("Also remove untracked files (clean checkout)")
        self.delete_old = QCheckBox("")
        for cb in (self.fetch, self.stash, self.backup, self.clean, self.delete_old):
            cb.toggled.connect(self.update_preview)
            lay.addWidget(cb)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        cancel.setDefault(True)
        self.ok = QPushButton("Reset")
        self.ok.setObjectName("danger")
        self.ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        lay.addLayout(row)
        self.preview = None
        self.update_preview()

    def update_preview(self):
        from . import rebase
        target = self.target.currentText()
        if not target:
            self.info.setText("No branch on origin to reset to.")
            self.ok.setEnabled(False)
            return
        if self.preview is None or self.preview.target != target:
            try:
                self.preview = rebase.reset_preview(self.p.path, target)
            except GitError as exc:
                self.info.setText(str(exc))
                self.ok.setEnabled(False)
                return
        pv = self.preview
        lines = [f"<b>{pv.local}</b> will match <b>{target}</b> exactly"
                 + (f" (you are on <b>{pv.current}</b> now)." if pv.current and pv.current != pv.local else ".")]
        if pv.lost_commits:
            listed = "".join(f"<br>&nbsp;&nbsp;• {c.sha[:8]} {c.subject}" for c in pv.lost_commits[:8])
            more = f"<br>&nbsp;&nbsp;… and {len(pv.lost_commits) - 8} more" if len(pv.lost_commits) > 8 else ""
            color = C["muted"] if self.backup.isChecked() else C["red"]
            lines.append(f"<span style='color:{color}'>{len(pv.lost_commits)} local commit(s) are not on "
                         f"{target}{' and will be lost' if not self.backup.isChecked() else ''}:{listed}{more}"
                         "</span>")
        if pv.changes and not self.stash.isChecked():
            lines.append(f"<span style='color:{C['red']}'>{pv.changes} uncommitted change(s) will be lost.</span>")
        self.info.setText("<br>".join(lines))
        self.stash.setVisible(bool(pv.changes))
        self.backup.setVisible(bool(pv.lost_commits))
        other = pv.current and pv.current != pv.local
        self.delete_old.setVisible(bool(other))
        if other:
            self.delete_old.setText(f"Delete my local branch {pv.current} after switching")
        self.ok.setEnabled(True)


class DeleteRepoDialog(QDialog):
    def __init__(self, parent, project, found_risks: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Delete repository")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(10)
        title = QLabel(f"Delete {project.title or project.name}?")
        title.setStyleSheet("font-size: 13pt; font-weight: 700;")
        lay.addWidget(title)
        lay.addWidget(_hint(f"The folder {project.path} is moved to the Windows Recycle Bin: you can still restore "
                            "it from there."))
        if found_risks:
            warn = QLabel("<b>This folder holds work found nowhere else:</b><br>• " + "<br>• ".join(found_risks))
            warn.setTextFormat(Qt.RichText)
            warn.setWordWrap(True)
            warn.setStyleSheet(f"background: rgba(242,107,107,0.10); border: 1px solid rgba(242,107,107,0.4);"
                               f"border-radius: 8px; padding: 10px 12px; color: {C['text']};")
            lay.addWidget(warn)
        else:
            lay.addWidget(_hint("Everything in it is committed and pushed."))
        self.forget = QCheckBox("Also remove its URL from the repository list (otherwise it shows as Not cloned)")
        self.forget.setChecked(True)
        self.forget.setVisible(bool(project.url))
        lay.addWidget(self.forget)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        cancel.setDefault(True)
        ok = QPushButton("Move to Recycle Bin")
        ok.setObjectName("danger")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
