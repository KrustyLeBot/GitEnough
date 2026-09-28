import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QGuiApplication, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy, QStackedWidget, QTableWidget,
                               QVBoxLayout, QWidget)

from . import __version__, discovery, git_ops, ides, rebase, repo, repo_dialog, updater, vault
from .branches_window import BranchesWindow
from .changes_window import ChangesWindow
from .compare_window import CompareWindow
from .config import Config
from .diff_view import warm_up_lexers
from .errors import explain
from .file_history_window import FileHistoryWindow
from .git_ops import Credential, Snapshot, folder_names, is_http, parse_url, same_repo
from .rebase_window import RebaseWindow
from .repo_dialog import DeleteRepoDialog, RepoSettingsDialog, ResetDialog
from .settings_dialog import SettingsDialog
from .stash_window import StashWindow
from .tree_window import TreeWindow
from .style import QSS, C, app_icon, arrow_pixmap, check_pixmap, chevron_pixmap, make_icon, pill_css
from .tasks import Tasks
from .update_toast import UpdateToast
from .watcher import RepoWatcher
from .widgets import ErrorDialog, NoWheelComboBox, confirm_discard_all, icon_button, set_css

FOCUS_REFRESH_DELAY = 4.0


@dataclass
class Project:
    url: str  # configured URL, or the repository's own origin for folders found on disk
    name: str
    host: str
    path: str
    snap: Snapshot | None = None
    busy: str = ""
    message: str = ""
    rel: str = ""  # path relative to the root folder
    title: str = ""  # display name (folder name, plus its parent when two folders share a name)
    discovered: bool = False  # found on disk, not from the configured URLs
    auto_title: str = ""  # title before any override from the repository settings

    @property
    def id(self) -> str:
        return os.path.normcase(os.path.normpath(self.path))

    @property
    def missing(self) -> bool:
        return self.snap is not None and self.snap.kind == "missing"

    @property
    def mismatch(self) -> bool:
        return self.snap is not None and self.snap.remote_mismatch

    @property
    def status(self):
        return self.snap.status if self.snap else None


FILTERS = [("all", "All"), ("attention", "Needs attention"), ("behind", "Behind"), ("changes", "Changes"),
           ("offbase", "Off base"), ("rebase", "Needs rebase"), ("uptodate", "Up to date"), ("missing", "Not cloned")]


def matches(p: Project, key: str) -> bool:
    snap, st = p.snap, p.status
    if key == "all":
        return True
    if snap is None:
        return False
    if key == "missing":
        return p.missing
    if key == "changes":
        return bool(st and st.changes)
    if key == "behind":
        return bool(st and st.behind and not p.mismatch)
    if key == "offbase":
        return bool(st and snap.base and st.branch and not snap.op and st.branch != snap.base)
    if key == "rebase":
        return bool(snap.base_behind)
    if key == "attention":
        return bool(snap.error or snap.op or p.mismatch or snap.kind == "notrepo"
                    or (st and ((st.ahead and st.behind) or st.upstream_gone)))
    if key == "uptodate":
        return bool(st and st.upstream and not st.upstream_gone and not st.ahead and not st.behind
                    and not snap.error and not p.mismatch)
    return True


def describe(p: Project) -> tuple[str, str, str]:
    """(label, color, tooltip) of the status pill."""
    snap, tip = p.snap, p.message
    if p.busy:
        return p.busy, C["blue"], ""
    if snap is None:
        return "…", C["grey"], ""
    if snap.kind == "missing":
        return "Not cloned", C["grey"], "Folder does not exist yet"
    if snap.kind == "notrepo" or snap.status is None:
        return "Error", C["red"], snap.error
    if snap.op:
        verb = {"rebase": "Rebasing", "merge": "Merging", "cherry-pick": "Cherry-picking", "revert": "Reverting",
                "bisect": "Bisecting", "am": "Applying patches"}.get(snap.op, snap.op)
        conflicts = snap.status.conflicts
        label = f"{verb} {snap.op_detail}".strip() + (f"  ·  {conflicts} conflict{'s' if conflicts > 1 else ''}"
                                                       if conflicts else "")
        what = f"{snap.op} of {snap.op_branch}" if snap.op_branch else snap.op
        how = {"rebase": "git rebase --continue / --skip / --abort", "merge": "git merge --continue / --abort",
               "cherry-pick": "git cherry-pick --continue / --abort", "revert": "git revert --continue / --abort",
               "bisect": "git bisect good / bad / reset", "am": "git am --continue / --abort"}.get(snap.op, "")
        tip = (f"A {what} is in progress" + (f" ({conflicts} conflicted file(s))" if conflicts else "")
               + f".\nFinish it in Git Bash: {how}\nPull, switch and Pull all are paused until then.")
        return label, C["op"], tip
    if snap.remote_mismatch:
        current = git_ops.mask_url(snap.origin) if snap.origin else "(none)"
        return "Remote mismatch", C["pink"], (f"Local origin: {current}\nConfigured: {p.url}\n"
                                             "Use Fix remote to point origin at the configured URL")
    if snap.error:
        return "Error", C["red"], snap.error
    st = snap.status
    if st.branch is None:
        return "Detached HEAD", C["violet"], tip
    if not p.url and not snap.origin:
        return "Local only", C["grey"], "This repository has no origin remote: nothing to pull or push"
    if not st.upstream or st.upstream_gone:
        on_origin = st.branch in snap.on_origin
        title = "Upstream gone" if st.upstream else "No upstream"
        why = (f"Tracked branch {st.upstream} no longer exists on the remote" if st.upstream
               else "Local branch has no remote tracking branch")
        if on_origin:
            fix = f"origin/{st.branch} exists: click Track to follow it"
        elif st.upstream and leave_target(p):
            fix = (f"The remote branch was probably merged and deleted: click Switch to {leave_target(p)} "
                   "(then pull). Right-click > Publish branch recreates it instead")
        else:
            fix = "The branch is not on origin: click Publish to push it"
        return title, C["violet"], f"{why}\n{fix}"
    if st.ahead and st.behind:
        return f"Diverged  ↑{st.ahead} ↓{st.behind}", C["red"], "Histories diverged: manual pull required"
    if st.behind:
        return f"Behind  ↓{st.behind}", C["orange"], tip or f"{st.behind} commit(s) to pull"
    if st.ahead:
        return f"Ahead  ↑{st.ahead}", C["blue"], tip or f"{st.ahead} commit(s) not pushed"
    return "Up to date", C["green"], tip


def leave_target(p: Project) -> str:
    """Base branch to go back to when the current branch's remote branch was deleted."""
    st, snap = p.status, p.snap
    if st and st.upstream_gone and snap.base and st.branch != snap.base and snap.base in snap.branches:
        return snap.base
    return ""


def cell(*widgets, center: bool = False, spacing: int = 8) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(12, 0, 12, 0)
    lay.setSpacing(spacing)
    if center:
        lay.addStretch()
    for widget in widgets:
        lay.addWidget(widget, 0, Qt.AlignVCenter)
    lay.addStretch()
    return w


class Row:
    """One project line. Its widgets are only built once the line scrolls into view (see ensure_rows)."""

    def __init__(self, win: "MainWindow", p: Project, index: int):
        self.win, self.p, self.index = win, p, index
        self.built = False

    def build(self):
        if self.built:
            return
        self.built = True
        win, p = self.win, self.p
        self._create(win, p)
        for col, widget in enumerate(self.cells):
            win.table.setCellWidget(self.index, col, widget)
        self.render()

    def _create(self, win: "MainWindow", p: Project):
        self.check = QCheckBox()
        self.check.setChecked(win.is_enabled(p))
        self.check.setToolTip("Include in Pull all")
        self.check.toggled.connect(self.on_toggle)

        self.name = QLabel(p.title or p.name)
        where = p.rel.replace(os.sep, "/")
        self.url = QLabel("  ·  ".join(x for x in (where if where != p.name else "", p.url or "no remote") if x))
        self.url.setObjectName("muted")
        info = QWidget()
        info_lay = QVBoxLayout(info)
        info_lay.setContentsMargins(0, 0, 0, 0)
        info_lay.setSpacing(1)
        info_lay.addWidget(self.name)
        info_lay.addWidget(self.url)
        self.url.setToolTip(p.path)
        info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        # Pills are buttons: an error pill opens its explanation, the changes pill opens the Changes window.
        self.pill = QPushButton()
        self.pill.clicked.connect(lambda: win.explain_error(p))
        self.branch = NoWheelComboBox()
        self.branch.setMinimumWidth(170)
        self.branch.setMaxVisibleItems(18)
        self.branch.activated.connect(self.on_branch)
        # Clickable: when the base moved on, it opens the rebase assistant preset for this branch.
        self.base_flag = QPushButton()
        self.base_flag.clicked.connect(self.on_base_flag)
        self.changes = QPushButton()
        self.changes.clicked.connect(lambda: win.open_changes(p))
        self.stash = QPushButton()
        self.stash.setCursor(Qt.PointingHandCursor)
        self.stash.setStyleSheet(pill_css(C["violet"]) + "border: none; padding: 3px 9px;")
        self.stash.clicked.connect(lambda: win.open_stashes(p))
        self.manage = icon_button("branches", "Manage branches")
        self.manage.clicked.connect(lambda: win.open_branches(p))
        self.vs = icon_button("vs", "Open solution in Visual Studio")
        self.vs.clicked.connect(lambda: win.open_solution(p, self.vs))
        self.code = icon_button("code", "Open folder in VS Code")
        self.code.clicked.connect(lambda: win.open_code(p.path))

        self.action = QPushButton()
        self.action.setObjectName("rowAction")
        self.action.setCursor(Qt.PointingHandCursor)
        self.action.clicked.connect(self.on_action)
        self.tree = icon_button("tree", "History (commit graph)")
        self.tree.clicked.connect(lambda: win.open_tree(p))
        self.bash = icon_button("terminal", "Open Git Bash here")
        self.bash.clicked.connect(lambda: win.open_bash(p.path))
        self.folder = icon_button("folder", "Open folder")
        self.folder.clicked.connect(lambda: win.open_path(p.path))
        self.web = icon_button("globe", "Open in browser")
        self.web.clicked.connect(lambda: win.open_web(p))
        self.gear = icon_button("gear", "Repository settings: remote URL, name, base branch, hide or delete")
        self.gear.clicked.connect(lambda: win.open_repo_settings(p))

        self.pin = icon_button("pin", "Pin to the top of the list")
        self.pin.setCheckable(True)
        self.pin.setChecked(p.id in win.config.pinned)
        self.pin.toggled.connect(lambda on: win.set_pinned(p, on))
        self.compare = icon_button("compare", "Compare with the base branch (merge request view)")
        self.compare.clicked.connect(lambda: win.open_compare(p))
        self.cells = [cell(self.pin, self.check, spacing=2), cell(info), cell(self.pill),
                      cell(self.branch, self.manage, self.compare, self.base_flag, spacing=3),
                      cell(self.changes, self.stash, spacing=6),
                      cell(self.action, self.tree, self.bash, self.vs, self.code, self.folder, self.web, self.gear,
                           spacing=1)]
        self.cells[1].layout().setStretch(0, 1)
        # Rows are disabled while busy; a disabled focused widget hands focus to the next one in the
        # table, and the table then scrolls to it (back to the top). Mouse-only widgets avoid that.
        for widget in (self.pin, self.compare, self.base_flag, self.check, self.pill, self.branch, self.manage, self.changes,
                       self.stash, self.action,
                       self.tree, self.bash, self.vs, self.code, self.folder, self.web, self.gear):
            widget.setFocusPolicy(Qt.NoFocus)

    @property
    def enabled(self) -> bool:
        return self.win.is_enabled(self.p)

    def on_toggle(self, checked: bool):
        self.win.set_enabled(self.p, checked)
        self.render()

    def on_branch(self, index: int):
        target = self.branch.itemText(index)
        st = self.p.status
        if st and target != st.branch and not target.startswith("("):
            self.win.switch_branch(self.p, target)

    def on_base_flag(self):
        snap = self.p.snap
        if snap and snap.base_behind:
            self.win.open_rebase(self.p, snap.status.branch, snap.base_ref)

    def on_action(self):
        kind = self.action_kind()
        if kind == "fix":
            self.win.fix_remote(self.p)
        elif kind == "track":
            self.win.set_upstream(self.p)
        elif kind == "leave":
            self.win.switch_branch(self.p, leave_target(self.p), then_pull=True)
        elif kind in ("push", "publish"):
            self.win.push_project(self.p)
        elif kind == "bash":
            if self.p.snap and self.p.snap.op == "rebase":
                self.win.open_rebase(self.p)
            else:
                self.win.open_bash(self.p.path)
        else:
            self.win.sync_project(self.p)

    def action_kind(self) -> str:
        p, st = self.p, self.p.status
        if not p.url and not p.missing:
            return "none"  # Local-only repository: nothing to pull from or push to.
        if p.mismatch:
            return "fix"
        if p.missing:
            return "clone"
        if p.snap and p.snap.op:
            return "bash"
        if st and st.branch and (not st.upstream or st.upstream_gone):
            if st.branch in p.snap.on_origin:
                return "track"
            return "leave" if leave_target(p) else "publish"
        if st and st.branch and st.ahead and not st.behind:
            return "push"
        return "pull"

    def render(self):
        if not self.built:
            return  # Rendered when it scrolls into view.
        p, st = self.p, self.p.status
        text, color, tip = describe(p)
        is_error = color == C["red"] and not p.busy and bool(p.snap and p.snap.error)
        self.pill.setText(f"●  {text}")
        set_css(self.pill, pill_css(color) + "border: none; text-align: left;")
        self.pill.setToolTip(tip + ("\n\nClick for details" if is_error else ""))
        self.pill.setCursor(Qt.PointingHandCursor if is_error else Qt.ArrowCursor)
        self.pill.setEnabled(True)

        dim = not self.enabled
        set_css(self.name, f"font-weight: 600; font-size: 10.5pt; color: {C['faint'] if dim else C['text']};")

        self.branch.blockSignals(True)
        self.branch.clear()
        if st:
            items = list(p.snap.branches)
            # During a rebase HEAD is detached: show the branch being rebased instead.
            shown = st.branch or (f"{p.snap.op_branch} ({p.snap.op})" if p.snap.op_branch else "(detached)")
            if st.branch is None:
                items.insert(0, shown)
            self.branch.addItems(items)
            self.branch.setCurrentText(shown)
        else:
            self.branch.addItem("—")
        self.branch.blockSignals(False)
        self.branch.setEnabled(bool(st) and not p.busy and not p.snap.op)
        off_base = bool(st and p.snap.base and st.branch and not p.snap.op and st.branch != p.snap.base)
        self.base_flag.setVisible(off_base)
        self.compare.setVisible(off_base)
        if off_base:
            behind, ref = p.snap.base_behind, p.snap.base_ref or p.snap.base
            if behind:
                self.base_flag.setText(f"↓{behind} {p.snap.base} · Rebase")
                set_css(self.base_flag, pill_css(C["orange"]) + "border: none; padding: 2px 9px; font-size: 8.5pt;")
                self.base_flag.setToolTip(f"{behind} commit(s) on {ref} are not in {st.branch}.\n"
                                          f"Click to rebase {st.branch} onto {ref}.")
                self.base_flag.setCursor(Qt.PointingHandCursor)
            else:
                self.base_flag.setText(f"≠ {p.snap.base}")
                set_css(self.base_flag, pill_css(C["grey"]) + "border: none; padding: 2px 9px; font-size: 8.5pt;")
                self.base_flag.setToolTip(f"Not on the base branch, but up to date with {ref}: no rebase needed")
                self.base_flag.setCursor(Qt.ArrowCursor)
        tips = [f"Tracks {st.upstream}"] if st and st.upstream else []
        if st and st.changes:
            tips.append("Local changes are carried over; on conflict you can stash and retry")
        self.branch.setToolTip("\n".join(tips))

        base = "border: none; text-align: left;"
        if not st:
            self.changes.setText("—")
            set_css(self.changes, f"{base} background: transparent; color: {C["faint"]}; padding: 3px 0; font-weight: 400;")
            self.changes.setToolTip("")
            self.changes.setCursor(Qt.ArrowCursor)
        elif st.changes:
            self.changes.setText(f"✎  {st.changes} change{'s' if st.changes > 1 else ''}")
            set_css(self.changes, pill_css(C["yellow"]) + base)
            self.changes.setToolTip("Modified, added or untracked files\nClick to review, stage and commit")
            self.changes.setCursor(Qt.PointingHandCursor)
        else:
            self.changes.setText("Clean")
            set_css(self.changes, f"{base} background: transparent; color: {C["muted"]}; padding: 3px 0; font-weight: 400;")
            self.changes.setToolTip("No local changes")
            self.changes.setCursor(Qt.ArrowCursor)
        self.changes.setEnabled(bool(st and st.changes))

        text, tooltip = {
            "fix": ("Fix remote", "Replace the origin URL with the configured URL, then fetch"),
            "clone": ("Clone", "Clone the repository"),
            "track": ("Track", f"Set origin/{st.branch if st else ''} as the upstream of this branch"),
            "leave": (f"Switch to {leave_target(p)}",
                      f"The remote branch is gone: switch to {leave_target(p)} and pull it"),
            "publish": ("Publish", "Push this branch to origin and track it"),
            "push": ("Push", "Push local commits"),
            "pull": ("Pull", "Fetch, then fast-forward if behind"),
            "bash": ("Resolve…", "Resolve the conflicts and continue the rebase")
            if p.snap and p.snap.op == "rebase" else ("Git Bash", "Open Git Bash to finish the operation in progress"),
            "none": ("No remote", "This repository has no origin remote"),
        }[self.action_kind()]
        self.action.setText(text)
        self.action.setToolTip(tooltip)
        self.action.setEnabled(not p.busy and p.snap is not None and p.snap.kind != "notrepo"
                               and self.action_kind() != "none")
        is_repo = p.snap is not None and p.snap.kind == "repo"
        self.folder.setVisible(p.snap is not None and not p.missing)
        # Visibility only here, once parented: setVisible(True) on a parentless widget opens it as a window.
        self.web.setVisible(git_ops.web_url(p.url) is not None)
        self.tree.setVisible(is_repo)
        self.bash.setVisible(is_repo)
        self.manage.setVisible(is_repo)
        self.vs.setVisible(is_repo and bool(p.snap.solutions))
        sols = p.snap.solutions if is_repo else []
        # With several solutions the button shows their count and opens a picker.
        self.vs.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if len(sols) > 1 else Qt.ToolButtonIconOnly)
        self.vs.setText(str(len(sols)) if len(sols) > 1 else "")
        if sols:
            self.vs.setToolTip(f"Open {sols[0]} in Visual Studio" if len(sols) == 1
                               else f"{len(sols)} solutions: pick one to open in Visual Studio")
        self.code.setVisible(is_repo and bool(ides.vscode_path()))
        n = p.snap.stashes if is_repo else 0
        self.stash.setVisible(bool(n))
        self.stash.setText(f"{n} stashed")
        self.stash.setToolTip("Stashed changes: click to review, apply or drop them")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = Config.load()
        self.tasks = Tasks()
        self.projects: list[Project] = []
        self.rows: list[Row] = []
        self.creds: dict[str, Credential] = {}
        self.windows: dict[tuple[str, str], QWidget] = {}
        self.bulk_total = self.bulk_done = self.bulk_errors = 0
        self.bulk_label = ""
        self.last_focus_refresh = time.monotonic()
        self.reload_gen = 0
        self.watcher: RepoWatcher | None = None
        try:
            self.watcher = RepoWatcher(self)
            self.watcher.changed.connect(self.on_files_changed)
        except Exception:  # noqa: BLE001 - no file watching: focus refresh and auto-fetch still work
            self.watcher = None

        self.setWindowTitle("GitEnough")
        self.setWindowIcon(make_icon())
        self.resize(1540, 760)
        if self.config.geometry:
            self.restoreGeometry(QByteArray.fromHex(self.config.geometry.encode()))
        self._build_ui()
        self.reload()
        # Slow first-time lookups (pygments index, vswhere) paid in the background, not on first click.
        self.tasks.submit(warm_up_lexers, lambda _r, _e: None)
        self.tasks.submit(ides.devenv_path, lambda _r, _e: None)
        self.fetch_timer = QTimer(self)
        self.fetch_timer.timeout.connect(self.auto_fetch)
        self.apply_auto_fetch()
        self.toast: UpdateToast | None = None
        # The previous exe (after a self-update) stays locked while that process exits: retry every
        # 2 s for 2 minutes.
        self._cleanup_tries = 0
        self.cleanup_timer = QTimer(self)
        self.cleanup_timer.timeout.connect(self._cleanup_old_exe)
        if not updater.cleanup_previous():
            self.cleanup_timer.start(2000)
        # Update check shortly after start (the list comes first), then every 6 hours.
        QTimer.singleShot(5000, self.check_updates)
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self.check_updates)
        self.update_timer.start(6 * 3600 * 1000)

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 12)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(app_icon(40))
        header.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("GitEnough")
        title.setObjectName("title")
        self.subtitle = QPushButton()
        self.subtitle.setObjectName("subtitle")
        self.subtitle.setFlat(True)
        self.subtitle.setCursor(Qt.PointingHandCursor)
        self.subtitle.setStyleSheet("text-align: left; padding: 0; border: none; background: transparent;"
                                    "font-weight: 400;")
        self.subtitle.clicked.connect(lambda: self.config.root and self.open_path(self.config.root))
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(220)
        self.search.textChanged.connect(self.apply_filter)
        self.refresh_btn = QPushButton("⟳  Refresh")
        self.refresh_btn.setToolTip("Fetch all checked projects")
        self.refresh_btn.clicked.connect(self.reload)  # Rescan for new folders, then fetch.
        self.pull_btn = QPushButton("⤓  Pull all")
        self.pull_btn.setObjectName("primary")
        self.pull_btn.setToolTip("Clone missing projects and update those behind (checked projects)")
        self.pull_btn.clicked.connect(self.pull_all)
        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Settings")
        settings_btn.clicked.connect(self.open_settings)
        for w in (self.search, self.refresh_btn, self.pull_btn, settings_btn):
            w.setCursor(Qt.PointingHandCursor)
            header.addWidget(w)
        root.addLayout(header)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.hide()
        root.addWidget(self.progress)

        # Clickable state filters with live counts (they replace a plain summary line).
        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.filter_group = QButtonGroup(self)
        self.filter_buttons: dict[str, QPushButton] = {}
        for key, label in FILTERS:
            btn = QPushButton(label)
            btn.setObjectName("filterChip")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setChecked(key == self.config.list_filter)
            btn.clicked.connect(lambda _=False, k=key: self.set_list_filter(k))
            self.filter_group.addButton(btn)
            self.filter_buttons[key] = btn
            filters.addWidget(btn)
        filters.addStretch()
        self.summary = QLabel()
        self.summary.setObjectName("summary")
        filters.addWidget(self.summary)
        root.addLayout(filters)

        self.stack = QStackedWidget()
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["✓", "Project", "Status", "Branch", "Changes", ""])
        self.table.horizontalHeaderItem(0).setToolTip("Check / uncheck all")
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.sectionClicked.connect(lambda i: i == 0 and self.toggle_all())
        for col, width in ((0, 70), (2, 250), (3, 360), (4, 210), (5, 320)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(60)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.verticalScrollBar().valueChanged.connect(lambda _v: self.ensure_rows())
        self.table.viewport().installEventFilter(self)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.stack.addWidget(self.table)

        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch()
        et = QLabel("No projects configured")
        et.setObjectName("emptyTitle")
        et.setAlignment(Qt.AlignCenter)
        es = QLabel("Set a root folder and add git URLs in the settings.")
        es.setObjectName("subtitle")
        es.setAlignment(Qt.AlignCenter)
        eb = QPushButton("Open settings")
        eb.setObjectName("primary")
        eb.clicked.connect(self.open_settings)
        el.addWidget(et)
        el.addWidget(es)
        el.addSpacing(10)
        el.addWidget(eb, 0, Qt.AlignCenter)
        el.addStretch()
        self.stack.addWidget(empty)
        root.addWidget(self.stack, 1)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().setContentsMargins(20, 0, 20, 4)
        self.statusBar().showMessage(f"v{__version__}")

    # ---------- data ----------
    def reload(self):
        """Scan the root folder in the background, then rebuild the list."""
        cfg = self.config
        git_ops.BASE_CANDIDATES = list(cfg.base_branches)
        git_ops.BASE_OVERRIDES = {k: v["base"] for k, v in cfg.repo_overrides.items() if v.get("base")}
        self.subtitle.setText(f"{cfg.root}  ↗" if cfg.root else "No root folder")
        if not cfg.root:
            self._show_projects([])
            return
        self.statusBar().showMessage("Scanning the root folder for repositories…")
        self.reload_gen += 1
        gen = self.reload_gen
        self.tasks.submit(discovery.discover, lambda res, err: self._on_discovered(gen, res, err),
                          cfg.root, list(cfg.repos))

    def _on_discovered(self, gen: int, result, error):
        if gen != self.reload_gen:
            return
        if error:
            self.statusBar().showMessage(f"Scan failed: {error}")
            return
        found, new_urls = result
        cfg = self.config
        if new_urls:
            cfg.repos = cfg.repos + new_urls
            cfg.save()
        root = os.path.normpath(cfg.root)
        # Existing Project objects are kept: open windows hold them, and their state stays on screen.
        old = {pr.id: pr for pr in self.projects}
        projects = []
        hidden = set(cfg.hidden)
        for f in found:
            if os.path.normcase(os.path.normpath(f.path)) in hidden:
                continue
            name = os.path.basename(f.path)
            rel = os.path.relpath(f.path, root) if f.path.lower().startswith(root.lower()) else f.path
            host = parse_url(f.url)[0] if f.url else ""
            pr = old.get(os.path.normcase(os.path.normpath(f.path)))
            if pr is None or pr.url != f.url:
                pr = Project(f.url, name, host, f.path, rel=rel, discovered=not f.configured)
            projects.append(pr)
        # Same folder name twice (two clones of one remote, or homonyms): add the parent folder to the title.
        counts: dict[str, int] = {}
        for pr in projects:
            counts[pr.name.lower()] = counts.get(pr.name.lower(), 0) + 1
        for pr in projects:
            parent = os.path.dirname(pr.rel)
            pr.title = f"{pr.name}  ·  {parent or 'root'}" if counts[pr.name.lower()] > 1 else pr.name
            pr.auto_title = pr.title
            pr.title = cfg.repo_overrides.get(pr.id, {}).get("title") or pr.title
        # The list is kept sorted, discovery is not: compare as sets of the very same objects.
        same = {id(pr) for pr in projects} == {id(pr) for pr in self.projects}
        if same and self.rows:
            self.refresh_all(fetch=True)  # Same repositories: only refresh their state.
        else:
            self._show_projects(projects)
        extra = f", {len(new_urls)} new URL(s) added to the repository list" if new_urls else ""
        self.statusBar().showMessage(f"{len(projects)} repositories{extra}", 6000)

    def _show_projects(self, projects: list[Project]):
        cfg = self.config
        self.projects = projects
        self.creds = {}
        for host in {p.host for p in self.projects if is_http(p.url)}:
            token = vault.get_token(host)
            if token:
                self.creds[host] = Credential(cfg.hosts.get(host) or vault.default_username(host), token)

        self.bulk_total = self.bulk_done = self.bulk_errors = 0
        self.progress.hide()
        alive = {id(pr) for pr in projects}
        for win in list(self.windows.values()):
            if id(getattr(win, "p", None)) not in alive:
                win.close()  # Its repository is gone from the list.
        self.table.setRowCount(0)
        self.rows = []
        ready = bool(cfg.root and self.projects)
        self.stack.setCurrentIndex(0 if ready else 1)
        self.pull_btn.setEnabled(ready)
        self.refresh_btn.setEnabled(ready)
        if not ready:
            self.update_summary()
            return
        self.build_rows()
        if self.watcher is not None:
            self.watcher.set_paths([pr.path for pr in self.projects] if cfg.watch_files else [])
        self.refresh_all(fetch=True)

    def build_rows(self):
        """(Re)create the rows: pinned projects first, then alphabetical."""
        pins = set(self.config.pinned)
        self.projects.sort(key=lambda pr: (pr.id not in pins, (pr.title or pr.name).lower(), pr.path.lower()))
        scroll = self.table.verticalScrollBar().value()
        # Built while hidden: every widget added to a visible table gets shown and styled one by one
        # (about 40 ms per row); hidden, it all happens once when the table is shown again.
        self.table.setUpdatesEnabled(False)
        self.table.hide()
        self.table.setRowCount(0)
        self.rows = []
        self.table.setRowCount(len(self.projects))
        self.rows = [Row(self, pr, i) for i, pr in enumerate(self.projects)]
        self.apply_filter()
        self.update_summary()
        self.table.show()
        self.table.setUpdatesEnabled(True)
        self.table.verticalScrollBar().setValue(scroll)
        self.ensure_rows()

    def ensure_rows(self):
        """Build the widgets of the rows on screen (plus a margin), so hundreds of repositories stay fast."""
        if not self.rows:
            return
        view = self.table.viewport()
        first = self.table.rowAt(0)
        last = self.table.rowAt(view.height() - 1)
        first = 0 if first < 0 else first
        last = len(self.rows) - 1 if last < 0 else last
        for i in range(max(0, first - 3), min(len(self.rows), last + 4)):
            if not self.table.isRowHidden(i):
                self.rows[i].build()

    def cred(self, p: Project) -> Credential | None:
        return self.creds.get(p.host) if is_http(p.url) else None

    def row_of(self, p: Project) -> Row | None:
        return next((r for r in self.rows if r.p is p), None)

    def apply(self, p: Project, snap: Snapshot | None, error: Exception | None):
        p.busy = ""
        if error:
            snap = Snapshot("repo" if p.snap and p.snap.kind == "repo" else "notrepo", error=repr(error))
        if snap:
            base = self.config.repo_overrides.get(p.id, {}).get("base")
            if base and snap.kind == "repo":
                snap.base = base  # Chosen in the repository settings, wins over the global priority list.
            p.snap = snap
            p.message = snap.message
        row = self.row_of(p)
        if row:
            row.render()
        self.update_summary()
        if self.config.list_filter != "all":
            self.apply_filter()

    def set_busy(self, p: Project, label: str):
        p.busy = label
        row = self.row_of(p)
        if row:
            row.render()

    def enabled_projects(self) -> list[Project]:
        return [r.p for r in self.rows if r.enabled]

    def set_pinned(self, p: Project, pinned: bool):
        pins = [x for x in self.config.pinned if x != p.id] + ([p.id] if pinned else [])
        self.config.pinned = pins
        self.config.save()
        self.build_rows()

    def set_enabled(self, p: Project, enabled: bool):
        # Entries are folder ids; URLs are legacy entries from before discovery (removed when toggled).
        disabled = [u for u in self.config.disabled if u not in (p.id, p.url)]
        if not enabled:
            disabled.append(p.id)
        self.config.disabled = disabled
        self.config.save()
        self.update_summary()

    def is_enabled(self, p: Project) -> bool:
        return p.id not in self.config.disabled and p.url not in self.config.disabled

    def toggle_all(self):
        target = not all(r.enabled for r in self.rows)
        for r in self.rows:
            if r.built:
                r.check.setChecked(target)  # Also saves through its toggled signal.
            elif r.enabled != target:
                self.set_enabled(r.p, target)

    # ---------- operations ----------
    def refresh_all(self, fetch: bool):
        """Local status first (instant), then fetch the checked projects in background."""
        enabled = set(map(id, self.enabled_projects()))
        targets = [p for p in self.projects if not p.busy]
        to_fetch = [p for p in targets if fetch and id(p) in enabled]
        if to_fetch:
            self.begin_bulk(len(to_fetch), "Refresh")
        for p in targets:
            do_fetch = p in to_fetch

            def done(snap, err, p=p, do_fetch=do_fetch):
                self.apply(p, snap, err)
                if do_fetch:
                    if p.snap and p.snap.kind == "repo":
                        self.set_busy(p, "Fetch…")
                        self.tasks.submit_network(git_ops.inspect, lambda s, e, p=p: self.bulk_step(p, s, e),
                                          p.url, p.path, self.cred(p), True)
                    else:
                        self.bulk_step(p, None, None)

            self.tasks.submit_status(git_ops.inspect, done, p.url, p.path, None, False)

    def pull_all(self):
        targets = [p for p in self.enabled_projects() if not p.busy]
        if not targets:
            self.statusBar().showMessage("No project checked", 4000)
            return
        os.makedirs(self.config.root, exist_ok=True)
        self.begin_bulk(len(targets), "Pull")
        for p in targets:
            self.set_busy(p, "Cloning…" if p.missing else "Pull…")
            self.tasks.submit_network(git_ops.sync, lambda s, e, p=p: self.bulk_step(p, s, e),
                              p.url, p.path, self.cred(p))

    def sync_project(self, p: Project, stash: bool = False):
        if p.busy:
            return
        self.set_busy(p, "Cloning…" if p.missing else "Pull…")
        self.tasks.submit_network(git_ops.sync,
                          lambda s, e: self.single_done(p, s, e, "Pull",
                                                        lambda: self.sync_project(p, stash=True)),
                          p.url, p.path, self.cred(p), stash)

    def switch_branch(self, p: Project, branch: str, stash: bool = False, then_pull: bool = False):
        if p.busy or not branch:
            return
        self.set_busy(p, "Switching…")

        def done(snap, err):
            self.single_done(p, snap, err, f"Switching to {branch}",
                             lambda: self.switch_branch(p, branch, stash=True, then_pull=then_pull))
            if then_pull and not err and snap and not snap.error:
                self.sync_project(p)

        self.tasks.submit(git_ops.switch, done, p.url, p.path, branch, self.cred(p), stash)

    def push_project(self, p: Project):
        if p.busy:
            return
        if p.mismatch:
            self.show_error(p, "Origin URL differs from the configured URL. Use Fix remote before pushing.", "Push")
            return
        self.set_busy(p, "Pushing…")
        self.tasks.submit_network(git_ops.push, lambda s, e: self.single_done(p, s, e, "Push"), p.url, p.path, self.cred(p))

    def fix_remote(self, p: Project):
        if p.busy:
            return
        current = git_ops.mask_url(p.snap.origin) if p.snap.origin else "(no origin remote)"
        answer = QMessageBox.question(
            self, "Fix remote",
            f"<b>{p.name}</b><br><br>Current origin:<br><code>{current}</code><br><br>"
            f"New origin:<br><code>{p.url}</code><br><br>"
            "Local branches and changes are kept. Continue?")
        if answer != QMessageBox.Yes:
            return
        self.set_busy(p, "Updating remote…")
        self.tasks.submit_network(git_ops.fix_remote, lambda s, e: self.single_done(p, s, e, "Fix remote"),
                          p.url, p.path, self.cred(p))

    def refresh_project(self, p: Project):
        if not p.busy:
            self.tasks.submit_status(git_ops.inspect, lambda s, e: self.apply(p, s, e), p.url, p.path, None, False)

    def single_done(self, p: Project, snap, err, action: str = "", retry=None):
        self.apply(p, snap, err)
        error = repr(err) if err else (snap.error if snap else "")
        if error:
            self.statusBar().showMessage(f"{p.name}: {action or 'operation'} failed", 6000)
            self.show_error(p, error, action, retry)
        else:
            self.statusBar().showMessage(f"{p.name}: {snap.message or 'OK'}", 6000)
        self.refresh_windows(p)

    def show_error(self, p: Project, error: str, action: str = "", retry=None):
        ex = explain(error, action or "This operation")
        label = "Stash && retry" if retry else ""
        if ErrorDialog(ex, f"{p.name}{f'  ·  {action}' if action else ''}", self, label).exec() and retry:
            retry()

    def explain_error(self, p: Project):
        # Error pills (typically from Pull all) open the same explanation, with a retry through stash.
        if p.snap and p.snap.error and not p.busy:
            self.show_error(p, p.snap.error, "Pull", lambda: self.sync_project(p, stash=True))

    def _open_window(self, kind: str, p: Project, factory):
        key = (kind, p.path)
        win = self.windows.get(key)
        if win is None:
            win = factory()
            win.setAttribute(Qt.WA_DeleteOnClose)
            win.destroyed.connect(lambda *_: self.windows.pop(key, None))
            self.windows[key] = win
        win.show()
        win.raise_()
        win.activateWindow()
        return win

    def set_upstream(self, p: Project):
        if p.busy:
            return
        self.set_busy(p, "Tracking…")
        self.tasks.submit(git_ops.set_upstream, lambda s, e: self.single_done(p, s, e, "Set upstream"),
                          p.url, p.path, self.cred(p))

    def open_web(self, p: Project, branch: bool = False):
        name = p.status.branch if branch and p.status else None
        url = git_ops.web_url(p.url, name)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def open_repo_settings(self, p: Project):
        dialog = RepoSettingsDialog(self, p)
        if not dialog.exec():
            return
        if dialog.action == "hide":
            self.hide_repo(p)
        elif dialog.action == "delete":
            self.delete_repo(p)
        elif dialog.action == "rebase":
            self.open_rebase(p)
        elif dialog.action == "reset":
            self.reset_to_remote(p)
        else:
            order = (p.title, p.id in self.config.pinned)
            dialog.apply()
            self.update_project(p, reorder=order != (self._title_of(p), p.id in self.config.pinned))

    def _load_creds(self):
        cfg = self.config
        self.creds = {}
        for host in {pr.host for pr in self.projects if is_http(pr.url)}:
            token = vault.get_token(host)
            if token:
                self.creds[host] = Credential(cfg.hosts.get(host) or vault.default_username(host), token)

    def _replace_url(self, old: str, new: str):
        """Swap a URL in the repository list (entries pointing at the same repository included)."""
        repos = [new if old and (u == old or same_repo(u, old)) else u for u in self.config.repos]
        if new not in repos:
            repos.append(new)
        self.config.repos = list(dict.fromkeys(repos))

    def change_remote_url(self, p: Project, url: str):
        old = (p.snap.origin if p.snap and p.snap.origin else "") or p.url
        self._replace_url(old, url)
        if p.url and p.url != old:
            self._replace_url(p.url, url)
        self.config.save()
        p.url, p.host = url, parse_url(url)[0]
        self._load_creds()
        row = self.row_of(p)
        if row and row.built:
            row.url.setText("  ·  ".join(x for x in (p.rel.replace(os.sep, "/") if p.rel != p.name else "", url) if x))
        if p.snap and p.snap.kind == "repo":
            self.set_busy(p, "Updating remote…")
            self.tasks.submit_network(git_ops.fix_remote, lambda s, e: self.single_done(p, s, e, "Change remote URL"),
                                      url, p.path, self.cred(p))

    def _title_of(self, p: Project) -> str:
        return self.config.repo_overrides.get(p.id, {}).get("title") or p.auto_title or p.name

    def update_project(self, p: Project, reorder: bool):
        """Apply a project's new settings without touching the others (no rescan, no fetch)."""
        git_ops.BASE_OVERRIDES = {k: v["base"] for k, v in self.config.repo_overrides.items() if v.get("base")}
        p.title = self._title_of(p)
        if reorder:
            self.build_rows()  # Name or pin changed: its position in the list may change.
        else:
            row = self.row_of(p)
            if row and row.built:
                row.name.setText(p.title)
                row.check.blockSignals(True)
                row.check.setChecked(self.is_enabled(p))
                row.check.blockSignals(False)
        self.refresh_project(p)  # Local status only: picks up the base branch override.
        self.update_summary()

    def remove_project(self, p: Project):
        """Drop one project from the list; the others keep their state."""
        for win in list(self.windows.values()):
            if getattr(win, "p", None) is p:
                win.close()
        self.projects = [x for x in self.projects if x is not p]
        if self.watcher is not None:
            self.watcher.set_paths([x.path for x in self.projects] if self.config.watch_files else [])
        self.build_rows()

    def hide_repo(self, p: Project):
        self.config.hidden = list(dict.fromkeys(self.config.hidden + [p.id]))
        self.config.save()
        self.statusBar().showMessage(f"{p.title or p.name} hidden (Settings > Repositories to show it again)", 8000)
        self.remove_project(p)

    def forget_url(self, p: Project):
        self.config.repos = [u for u in self.config.repos if u != p.url]
        self.config.save()
        self.remove_project(p)

    def delete_repo(self, p: Project):
        if p.busy or not os.path.isdir(p.path):
            return
        self.statusBar().showMessage(f"Checking {p.title or p.name} for unpushed work…")
        self.tasks.submit(repo_dialog.risks, lambda found, err: self._confirm_delete(p, found or []), p.path)

    def _confirm_delete(self, p: Project, found: list[str]):
        self.statusBar().clearMessage()
        dialog = DeleteRepoDialog(self, p, found)
        if not dialog.exec():
            return
        forget = dialog.forget.isChecked()
        for key, win in list(self.windows.items()):
            if getattr(win, "p", None) is p:
                win.close()
        if self.watcher is not None:
            # Release the folder handle first, or Windows refuses to move the folder.
            self.watcher.set_paths([pr.path for pr in self.projects if pr is not p])
        self.set_busy(p, "Deleting…")

        def done(_r, err):
            p.busy = ""
            if err:
                QMessageBox.warning(self, "Delete repository", str(err))
                self.reload()
                return
            cfg = self.config
            if forget and p.url:
                cfg.repos = [u for u in cfg.repos if not (u == p.url or same_repo(u, p.url))]
            cfg.pinned = [x for x in cfg.pinned if x != p.id]
            cfg.disabled = [x for x in cfg.disabled if x != p.id]
            cfg.repo_overrides.pop(p.id, None)
            cfg.save()
            self.statusBar().showMessage(f"{p.title or p.name} moved to the Recycle Bin", 8000)
            self.remove_project(p)

        self.tasks.submit(repo_dialog.move_to_recycle_bin, done, p.path)

    def open_bash(self, path: str):
        if not open_git_bash(path):
            QMessageBox.warning(self, "Git Bash", "Git Bash was not found next to git.exe.")

    def discard_all(self, p: Project, parent=None):
        if p.busy or not (p.snap and p.snap.kind == "repo"):
            return
        count = p.status.changes if p.status else 0
        choice = confirm_discard_all(parent or self, p.name, count)
        if not choice:
            return
        fn, label = (repo.stash_all, "Stash") if choice == "stash" else (repo.discard_all, "Discard all")
        self.set_busy(p, "Stashing…" if choice == "stash" else "Discarding…")

        def done(_r, err):
            p.busy = ""
            if err:
                self.show_error(p, str(err), label)
            else:
                self.statusBar().showMessage(
                    f"{p.name}: changes stashed (git stash pop to restore)" if choice == "stash"
                    else f"{p.name}: all changes discarded", 8000)
            self.refresh_project(p)
            self.refresh_windows(p)

        self.tasks.submit(fn, done, p.path)

    def open_changes(self, p: Project):
        if p.snap and p.snap.kind == "repo":
            def make():
                w = ChangesWindow(self, p)
                w.changed.connect(lambda: self.refresh_project(p))
                return w
            self._open_window("changes", p, make)

    def open_tree(self, p: Project):
        if p.snap and p.snap.kind == "repo":
            self._open_window("tree", p, lambda: TreeWindow(self, p))

    def open_compare(self, p: Project):
        if p.snap and p.snap.kind == "repo":
            self._open_window("compare", p, lambda: CompareWindow(self, p))

    def open_file_history(self, p: Project, file: str):
        if p.snap and p.snap.kind == "repo":
            self._open_window(f"file:{file}", p, lambda: FileHistoryWindow(self, p, file))

    def on_files_changed(self, path: str):
        key = os.path.normcase(os.path.normpath(path))
        pr = next((x for x in self.projects if x.id == key), None)
        if pr is not None and not pr.busy and not self.bulk_total:
            self.refresh_project(pr)
            self.refresh_windows(pr)

    def open_rebase(self, p: Project, branch: str | None = None, onto: str | None = None):
        if p.snap and p.snap.kind == "repo":
            win = self._open_window("rebase", p, lambda: RebaseWindow(self, p, branch, onto))
            win.destroyed.connect(lambda *_: self.refresh_windows(p))

    def open_merge_tool(self, p: Project, file: str):
        from .merge_tool import MergeToolWindow
        left, right = rebase.conflict_sides(p.path)
        win = self._open_window(f"merge:{file}", p, lambda: MergeToolWindow(self, p, file, left, right))
        win.resolved.connect(lambda _f: (self.refresh_project(p), self.refresh_windows(p)))

    def reset_to_remote(self, p: Project):
        if p.busy or not (p.snap and p.snap.kind == "repo"):
            return
        dialog = ResetDialog(self, p)
        if not dialog.exec():
            return
        target = dialog.target.currentText()
        opts = (dialog.stash.isChecked(), dialog.backup.isChecked(), dialog.clean.isChecked(),
                dialog.delete_old.isChecked() and dialog.delete_old.isVisible())
        fetch = dialog.fetch.isChecked()
        cred = self.cred(p)
        self.set_busy(p, "Resetting…")

        def work():
            if fetch:
                git_ops.run_git(["fetch", "--prune"], cwd=p.path, cred=cred, timeout=180)
            return rebase.reset_to_remote(p.path, target, *opts)

        def done(report, err):
            p.busy = ""
            self.refresh_project(p)
            self.refresh_windows(p)
            if err:
                self.show_error(p, str(err), "Reset to remote branch")
            else:
                self.statusBar().showMessage(f"{p.title or p.name}: {report}", 10000)

        (self.tasks.submit_network if fetch else self.tasks.submit)(work, done)

    def open_stashes(self, p: Project):
        if p.snap and p.snap.kind == "repo":
            self._open_window("stashes", p, lambda: StashWindow(self, p))

    def open_branches(self, p: Project):
        if p.snap and p.snap.kind == "repo":
            self._open_window("branches", p, lambda: BranchesWindow(self, p))

    def refresh_windows(self, p: Project, exclude=None):
        for win in list(self.windows.values()):
            if getattr(win, "p", None) is p and win is not exclude and not isinstance(win, TreeWindow):
                win.refresh()

    def open_solution(self, p: Project, anchor=None):
        sols = p.snap.solutions if p.snap else []
        if len(sols) == 1:
            self._launch_solution(p, sols[0])
        elif sols:
            menu = QMenu(self)
            for sol in sols:
                act = QAction(sol, menu)
                act.triggered.connect(lambda _=False, sol=sol: self._launch_solution(p, sol))
                menu.addAction(act)
            menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()) if anchor else self.cursor().pos())

    def _launch_solution(self, p: Project, sol: str):
        if not ides.open_solution(os.path.join(p.path, sol)):
            QMessageBox.warning(self, "Visual Studio", "Could not open the solution.")

    def open_code(self, path: str):
        if not ides.open_vscode(path):
            QMessageBox.warning(self, "VS Code", "VS Code was not found (code.exe or the code command).")

    def abort_operation(self, p: Project):
        op = p.snap.op if p.snap else ""
        if not op or p.busy:
            return
        box = QMessageBox(QMessageBox.Warning, f"Abort {op}",
                          f"Abort the {op} in progress in {p.name}?\n\nThe repository goes back to its state "
                          f"before the {op}; conflict resolutions made so far are lost.", QMessageBox.Cancel, self)
        confirm = box.addButton(f"Abort {op}", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is confirm:
            self.set_busy(p, "Aborting…")
            self.tasks.submit(git_ops.abort_operation, lambda s, e: self.single_done(p, s, e, f"Abort {op}"),
                              p.url, p.path, op, self.cred(p))

    def auto_fetch(self):
        if self.rows and not self.bulk_total and not QApplication.activeModalWidget():
            self.refresh_all(fetch=True)
            self.statusBar().showMessage(f"Auto-fetch at {time.strftime('%H:%M')}", 5000)

    def apply_auto_fetch(self):
        minutes = self.config.auto_fetch_minutes
        if minutes > 0:
            self.fetch_timer.start(minutes * 60 * 1000)
        else:
            self.fetch_timer.stop()

    def begin_bulk(self, total: int, label: str):
        if self.bulk_total and self.bulk_done < self.bulk_total:
            self.bulk_total += total  # Merge with a running batch.
        else:
            self.bulk_total, self.bulk_done, self.bulk_errors = total, 0, 0
            self.bulk_label = label
        self.progress.setRange(0, self.bulk_total)
        self.progress.setValue(self.bulk_done)
        self.progress.show()
        self.pull_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.statusBar().showMessage(f"{self.bulk_label}… 0/{self.bulk_total}")

    def bulk_step(self, p: Project, snap, err):
        if not any(p is q for q in self.projects):
            return  # Result from before a settings reload.
        self.apply(p, snap, err)
        self.bulk_done += 1
        if err or (snap and snap.error):
            self.bulk_errors += 1
        self.progress.setValue(self.bulk_done)
        if self.bulk_done < self.bulk_total:
            self.statusBar().showMessage(f"{self.bulk_label}… {self.bulk_done}/{self.bulk_total}")
            return
        self.progress.hide()
        self.pull_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        errors = f" · {self.bulk_errors} error(s), click a red status for details" if self.bulk_errors else ""
        self.statusBar().showMessage(f"{self.bulk_label} finished at {time.strftime('%H:%M')}{errors}")
        self.bulk_total = 0

    # ---------- misc ----------
    def set_list_filter(self, key: str):
        self.config.list_filter = key
        self.config.save()
        self.apply_filter()

    def update_summary(self):
        counts = {key: sum(1 for pr in self.projects if matches(pr, key)) for key, _ in FILTERS}
        for key, label in FILTERS:
            self.filter_buttons[key].setText(f"{label}  {counts[key]}")
            # Empty filters stay visible only while selected, so the row is not cluttered.
            self.filter_buttons[key].setVisible(key in ("all", self.config.list_filter) or counts[key] > 0)
        off = len(self.projects) - len(self.enabled_projects())
        self.summary.setText(f"{off} unchecked" if off and self.rows else "")

    def apply_filter(self):
        needle = self.search.text().strip().lower()
        state = self.config.list_filter
        for i, r in enumerate(self.rows):
            text_ok = not needle or needle in f"{r.p.title} {r.p.rel} {r.p.url}".lower()
            self.table.setRowHidden(i, not (text_ok and matches(r.p, state)))
        QTimer.singleShot(0, self.ensure_rows)

    def context_menu(self, pos):
        index = self.table.rowAt(pos.y())
        if index < 0 or index >= len(self.rows):
            return
        p = self.rows[index].p
        menu = QMenu(self)
        is_repo = bool(p.snap and p.snap.kind == "repo")
        actions = [
            ("Settings…", lambda: self.open_repo_settings(p), True),
            ("Unpin" if p.id in self.config.pinned else "Pin to top",
             lambda: self.set_pinned(p, p.id not in self.config.pinned), True),
            (None, None, True),
            ("Changes…", lambda: self.open_changes(p), is_repo),
            ("Compare with base…", lambda: self.open_compare(p), is_repo),
            ("Rebase onto…", lambda: self.open_rebase(p), is_repo and (not p.snap.op or p.snap.op == "rebase")),
            ("Reset to remote branch…", lambda: self.reset_to_remote(p), is_repo and not p.snap.op),
            ("History…", lambda: self.open_tree(p), is_repo),
            ("Branches…", lambda: self.open_branches(p), is_repo),
            ("Stashes…", lambda: self.open_stashes(p), is_repo),
            (None, None, True),
            (f"Abort {p.snap.op}…" if p.snap and p.snap.op else "Abort operation…",
             lambda: self.abort_operation(p), bool(p.snap and p.snap.op)),
            ("Open solution in Visual Studio", lambda: self.open_solution(p), bool(is_repo and p.snap.solutions)),
            (None, None, True),
            ("Publish branch", lambda: self.push_project(p),
             bool(p.status and p.status.branch and (not p.status.upstream or p.status.upstream_gone))),
            (None, None, True),
            ("Open in browser", lambda: self.open_web(p), git_ops.web_url(p.url) is not None),
            ("Open branch in browser", lambda: self.open_web(p, branch=True),
             bool(git_ops.web_url(p.url) and p.status and p.status.branch
                  and p.status.branch in p.snap.on_origin)),
            (None, None, True),
            ("Open folder", lambda: self.open_path(p.path), not p.missing),
            ("Open Git Bash", lambda: self.open_bash(p.path), is_repo),
            ("Open in terminal", lambda: open_terminal(p.path), is_repo),
            ("Open in VS Code", lambda: self.open_code(p.path), is_repo and bool(ides.vscode_path())),
            (None, None, True),
            ("Copy path", lambda: QGuiApplication.clipboard().setText(p.path), True),
            ("Copy URL", lambda: QGuiApplication.clipboard().setText(p.url), True),
            (None, None, True),
            ("Discard all changes…", lambda: self.discard_all(p), bool(p.status and p.status.changes)),
            (None, None, True),
            ("Hide from GitEnough", lambda: self.hide_repo(p), not p.missing),
            ("Remove from the list", lambda: self.forget_url(p), p.missing),
            ("Delete from disk…", lambda: self.delete_repo(p), not p.missing),
        ]
        for text, fn, enabled in actions:
            if text is None:
                menu.addSeparator()
                continue
            act = QAction(text, menu)
            act.setEnabled(enabled)
            act.triggered.connect(fn)
            menu.addAction(act)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def open_path(self, path: str):
        if os.path.isdir(path):
            os.startfile(path)

    def open_settings(self):
        dialog = SettingsDialog(self.config, self.tasks, self)
        if dialog.exec():
            self.apply_auto_fetch()
            self.reload()

    def _cleanup_old_exe(self):
        self._cleanup_tries += 1
        if updater.cleanup_previous() or self._cleanup_tries >= 60:
            self.cleanup_timer.stop()

    def check_updates(self, manual: bool = False):
        def done(release, err):
            if err:
                if manual:
                    QMessageBox.warning(self, "Check for updates", f"Could not check for updates:\n{err}")
                return
            if release is None:
                if manual:
                    QMessageBox.information(self, "Check for updates",
                                            f"You have the latest version ({__version__}).")
                return
            if self.toast is None or self.toast.release.version != release.version:
                self.toast = UpdateToast(self, release)
            self.toast.place()
            self.toast.show()

        self.tasks.submit_network(updater.check, done)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.toast is not None and self.toast.isVisible():
            self.toast.place()  # Stays pinned to the top-right corner.

    def eventFilter(self, obj, event):
        if obj is self.table.viewport() and event.type() == QEvent.Resize:
            QTimer.singleShot(0, self.ensure_rows)
        return super().eventFilter(obj, event)

    def changeEvent(self, event):
        # Coming back to the window: re-read local status to catch edits made elsewhere.
        watching = self.watcher is not None and self.config.watch_files and bool(self.watcher.watches)
        # With file watching on, repositories refresh on their own: no need to rescan them all on focus.
        if (event.type() == event.Type.ActivationChange and self.isActiveWindow() and self.rows and not watching
                and not self.bulk_total and time.monotonic() - self.last_focus_refresh > FOCUS_REFRESH_DELAY):
            self.last_focus_refresh = time.monotonic()
            self.refresh_all(fetch=False)
        super().changeEvent(event)

    def closeEvent(self, event):
        if self.watcher is not None:
            self.watcher.stop()
        self.config.geometry = self.saveGeometry().toHex().data().decode()
        self.config.save()
        git_ops.kill_all()
        self.tasks.shutdown()
        super().closeEvent(event)


def git_bash_path() -> str | None:
    git = shutil.which("git")
    roots = [os.path.dirname(os.path.dirname(git))] if git else []
    roots += [os.path.join(os.environ.get(v, ""), "Git") for v in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA")]
    for root in roots:
        exe = os.path.join(root, "git-bash.exe")
        if os.path.isfile(exe):
            return exe
    return None


def open_git_bash(path: str) -> bool:
    exe = git_bash_path()
    if not exe:
        return False
    subprocess.Popen([exe, f"--cd={path}"])
    return True


def open_terminal(path: str):
    if shutil.which("wt"):
        subprocess.Popen(["wt", "-d", path])
    else:
        subprocess.Popen(["cmd.exe", "/K"], cwd=path, creationflags=subprocess.CREATE_NEW_CONSOLE)


def apply_style(app: QApplication):
    # Fusion: the look comes from the stylesheet; the native Windows 11 style adds its own accent bars on top.
    app.setStyle("Fusion")
    # Dark palette underneath the stylesheet: Fusion's defaults are light, and any widget the stylesheet
    # does not cover (scroll area viewports, popups) would otherwise flash white.
    pal = QPalette()
    for role, color in ((QPalette.Window, C["bg"]), (QPalette.Base, C["surface"]), (QPalette.AlternateBase,
                        C["surface2"]), (QPalette.Text, C["text"]), (QPalette.WindowText, C["text"]),
                        (QPalette.Button, C["surface2"]), (QPalette.ButtonText, C["text"]),
                        (QPalette.Highlight, "#34406b"), (QPalette.HighlightedText, C["text"]),
                        (QPalette.ToolTipBase, C["surface2"]), (QPalette.ToolTipText, C["text"]),
                        (QPalette.PlaceholderText, C["faint"])):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    # QSS needs image files for the checkbox tick and combo arrow; render them once to temp.
    qss = QSS
    for key, pixmap in (("check", check_pixmap()), ("arrow", arrow_pixmap()),
                        ("branch-closed", chevron_pixmap("right")), ("branch-open", chevron_pixmap("down"))):
        path = os.path.join(tempfile.gettempdir(), f"gitenough_{key}.png")
        pixmap.save(path)
        qss = qss.replace(f":/{key}.png", path.replace("\\", "/"))
    app.setStyleSheet(qss)


def main():
    if sys.platform == "win32":
        import ctypes
        # Own taskbar identity so Windows shows our icon rather than python's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GitEnough.App")
    app = QApplication(sys.argv)
    app.setApplicationName("GitEnough")
    app.setWindowIcon(make_icon())
    apply_style(app)
    if not git_ops.git_available():
        QMessageBox.critical(None, "GitEnough", "git not found. Install Git for Windows, then restart.")
        return 1
    win = MainWindow()
    win.show()
    return app.exec()
