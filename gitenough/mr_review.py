"""Review of one GitLab merge request: files, diff, discussions, pending comments and AI proposals."""

import time
import uuid

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from . import ai, ai_review, gitlab, review_skill, vault
from .diff_view import DiffView, parse_diff
from .file_view import ExtensionBar, FileView, extension
from .pipeline_view import PipelinePage, chip_style
from .git_ops import Credential, GitError, run_git
from .repo import FileChange
from .style import C
from .widgets import ElidedLabel, Spinner, ai_error_dialog, icon_button, keep_size

SEVERITY_COLORS = {"critical": C["red"], "major": C["orange"], "minor": C["yellow"], "suggestion": C["blue"],
                   "summary": C["violet"]}
OVERVIEW = "\0overview"
PIPELINE = {  # status -> (label, colour key)
    "success": ("✓  Pipeline passed", "green"), "failed": ("✕  Pipeline failed", "red"),
    "running": ("●  Pipeline running", "blue"), "pending": ("●  Pipeline pending", "orange"),
    "created": ("●  Pipeline created", "orange"), "waiting_for_resource": ("●  Pipeline waiting", "orange"),
    "preparing": ("●  Pipeline preparing", "orange"), "scheduled": ("●  Pipeline scheduled", "orange"),
    "manual": ("▶  Pipeline waiting for a manual job", "orange"), "canceled": ("–  Pipeline canceled", "faint"),
    "skipped": ("–  Pipeline skipped", "faint"),
}
PIPELINE_VIEW = "\0pipeline"  # pseudo path shown in the centre area
PIPELINE_ACTIVE = {"running", "pending", "created", "waiting_for_resource", "preparing", "scheduled"}
MERGE = {  # detailed_merge_status -> (label, colour key)
    "mergeable": ("Ready to merge", "green"), "not_approved": ("Needs approval", "orange"),
    "ci_must_pass": ("Pipeline must pass", "orange"), "ci_still_running": ("Waiting for the pipeline", "blue"),
    "discussions_not_resolved": ("Unresolved threads", "orange"), "draft_status": ("Draft", "faint"),
    "conflict": ("Conflicts", "red"), "need_rebase": ("Needs rebase", "orange"),
    "requested_changes": ("Changes requested", "red"), "blocked_status": ("Blocked by another merge request", "red"),
    "checking": ("Checking mergeability…", "faint"), "unchecked": ("Checking mergeability…", "faint"),
    "preparing": ("Checking mergeability…", "faint"), "not_open": ("Not open", "faint"),
    "can_be_merged": ("Ready to merge", "green"), "cannot_be_merged": ("Cannot be merged", "red"),
}
  # pseudo path of the merge request's general discussion


def when(stamp: str) -> str:
    """'2026-09-29T08:12:00.000Z' -> '3 h ago' (rough, for lists)."""
    try:
        t = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (ValueError, OverflowError):
        return stamp[:10]
    s = max(0, time.time() - t)
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= size:
            return f"{int(s // size)} {unit} ago"
    return "just now"


class _Bridge(QObject):
    progress = Signal(object, int, str)
    found = Signal(list)


def _markdown(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.MarkdownText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextBrowserInteraction)
    label.setOpenExternalLinks(True)
    return label


class Card(QFrame):
    """One thread, pending comment or proposal in the side panel."""

    def __init__(self, kind: str, title: str, color: str, on_title=None):
        super().__init__()
        self.setObjectName("commentCard")
        self.setStyleSheet(f"QFrame#commentCard {{ background: {C['surface']}; border: 1px solid {C['border']};"
                           f" border-left: 3px solid {color}; border-radius: 8px; }}")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(12, 9, 12, 10)
        self.lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        chip = QLabel(kind)
        chip.setStyleSheet(f"color: {color}; font-weight: 700; font-size: 8pt;")
        head.addWidget(chip)
        where = QPushButton(title)
        where.setFlat(True)
        where.setCursor(Qt.PointingHandCursor)
        where.setStyleSheet(f"color: {C['muted']}; border: none; background: transparent; padding: 0;"
                            "text-align: left; font-size: 8.5pt;")
        where.setToolTip("Show in the diff")
        if on_title:
            where.clicked.connect(on_title)
        head.addWidget(where, 1)
        self.lay.addLayout(head)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(6)
        self.buttons.addStretch()

    def add_button(self, text: str, fn, primary: bool = False, danger: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("primary" if primary else "dangerSmall" if danger else "rowAction")
        if primary:
            b.setStyleSheet("padding: 4px 12px; font-size: 9pt;")
        b.clicked.connect(fn)
        self.buttons.addWidget(b)
        return b

    def finish(self):
        self.lay.addLayout(self.buttons)


class Editor(QWidget):
    """Inline text box with a confirm button, used for new comments, edits and replies."""

    def __init__(self, text: str, ok_label: str, on_ok, on_cancel, placeholder: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edit = QPlainTextEdit(text)
        self.edit.setPlaceholderText(placeholder or "Markdown supported · Ctrl+Enter to confirm")
        self.edit.setMinimumHeight(90)
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setObjectName("rowAction")
        cancel.clicked.connect(on_cancel)
        ok = QPushButton(ok_label)
        ok.setObjectName("primary")
        ok.setStyleSheet("padding: 4px 12px; font-size: 9pt;")
        ok.clicked.connect(lambda: self.edit.toPlainText().strip() and on_ok(self.edit.toPlainText().strip()))
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        QShortcut(QKeySequence("Ctrl+Return"), self.edit, ok.click).setContext(Qt.WidgetShortcut)
        QShortcut(QKeySequence("Escape"), self.edit, on_cancel).setContext(Qt.WidgetShortcut)
        QTimer.singleShot(0, self.edit.setFocus)


class ReviewWindow(QWidget):
    def __init__(self, main, mr: gitlab.MergeRequest):
        super().__init__(main, Qt.Window)
        self.main, self.tasks, self.mr = main, main.tasks, mr
        self.client: gitlab.Client | None = None
        self.files: list[gitlab.FileDiff] = []
        self.by_path: dict[str, gitlab.FileDiff] = {}
        self.parsed: dict[str, object] = {}
        self.discussions: list[gitlab.Discussion] = []
        self.current = ""  # path shown, or OVERVIEW
        self.composer = None  # (path, side, line) of the comment being written
        self._composer_widget = None
        self.editing = ""  # id of the pending comment / proposal being edited
        self.replying = ""  # discussion id with an open reply box
        self.busy = False
        self.ai_run: ai_review.Run | None = None
        self.ai_started = 0.0
        self.remote_drafts = 0
        self.checkout = None
        store = gitlab.load_state().get(mr.key, {})
        self.viewed: dict[str, str] = dict(store.get("viewed", {}))  # path -> diff signature when viewed
        self.pending: list[dict] = list(store.get("pending", []))  # comments written here, not sent yet
        self.summary = store.get("summary", "")
        self.status_timer = QTimer(self, interval=20_000)  # while the pipeline runs
        self.status_timer.timeout.connect(self.refresh_status)
        self.bridge = _Bridge()
        self.bridge.progress.connect(self._on_ai_progress)
        self.bridge.found.connect(self._on_ai_found)

        self.setWindowTitle(f"!{mr.iid} · {mr.title or mr.project}")
        self.resize(1600, 920)
        keep_size(self, main.config, "merge_request")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._header())
        root.addWidget(self._progress_row())

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(1)
        split.addWidget(self._left())
        self.diff = DiffView()
        self.diff.set_commentable(True)
        self.diff.comment_requested.connect(self.start_comment)
        self.diff.options_changed.connect(lambda: self.show_file(self.current, keep=True))
        self.center = QStackedWidget()
        self.center.addWidget(self.diff)
        self.center.addWidget(self._overview_page())
        self.pipeline_page = PipelinePage(self.tasks)
        self.center.addWidget(self.pipeline_page)
        split.addWidget(self.center)
        split.setStretchFactor(1, 1)
        split.setSizes([360, 1240])
        root.addWidget(split, 1)

        QShortcut(QKeySequence("F5"), self, self.load)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.search.setFocus())
        self.load()

    # ---------- layout ----------
    def _header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 12, 16, 12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = ElidedLabel(f"!{self.mr.iid}  {self.mr.title}", Qt.ElideRight)
        self.title.setStyleSheet("font-size: 13pt; font-weight: 700;")
        self.sub = ElidedLabel("", Qt.ElideRight)
        self.sub.setObjectName("muted")
        sub_row = QHBoxLayout()
        sub_row.setSpacing(8)
        self.loading_bar = Spinner(14)  # while the merge request loads
        self.loading_bar.hide()
        sub_row.addWidget(self.loading_bar)
        sub_row.addWidget(self.sub, 1)
        titles.addWidget(self.title)
        titles.addLayout(sub_row)
        # Pipeline, approvals and merge state, as small chips under the title.
        chips = QHBoxLayout()
        chips.setContentsMargins(0, 4, 0, 0)
        chips.setSpacing(6)
        self.pipeline_chip = QPushButton("")
        self.pipeline_chip.setCursor(Qt.PointingHandCursor)
        self.pipeline_chip.setToolTip("Show the pipeline: stages, jobs and their logs")
        self.pipeline_chip.clicked.connect(self.show_pipeline)
        self.approvals_chip = QLabel("")
        self.merge_chip = QLabel("")
        for w in (self.pipeline_chip, self.approvals_chip, self.merge_chip):
            w.hide()
            chips.addWidget(w)
        chips.addStretch()
        titles.addLayout(chips)
        hl.addLayout(titles, 1)
        web = icon_button("globe", "Open in GitLab")
        web.clicked.connect(lambda: self.mr.web_url and QDesktopServices.openUrl(QUrl(self.mr.web_url)))
        refresh = icon_button("refresh", "Reload (F5)")
        refresh.clicked.connect(self.load)
        self.ai_btn = QPushButton("✨ AI review")
        self.ai_btn.setToolTip(f"Review the merge request with Claude ({self.main.config.ai_review_model or 'default'}"
                               ") and propose comments you validate before sending")
        self.ai_btn.clicked.connect(self.start_ai)
        self.send_btn = QPushButton("Send comments")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self.send)
        self.approve_btn = QPushButton("✓ Approve")
        self.approve_btn.clicked.connect(self.toggle_approve)
        self.approve_btn.hide()
        self.ai_left = QLabel("")
        self.ai_left.setObjectName("muted")
        self.ai_next = QPushButton("Next proposal ▸")
        self.ai_next.setToolTip("Go to the next AI proposal to accept, edit or dismiss")
        self.ai_next.clicked.connect(self.next_proposal)
        self.file_comment = QPushButton("Comment on file")
        self.file_comment.setToolTip("A comment on the whole file shown (click a line number for a line)")
        self.file_comment.clicked.connect(lambda: self.start_comment(None))
        for w in (self.ai_left, self.ai_next, self.file_comment, web, refresh, self.ai_btn, self.approve_btn,
                  self.send_btn):
            hl.addWidget(w)
        return header

    def _progress_row(self) -> QWidget:
        self.progress_row = QWidget()
        self.progress_row.setObjectName("selBar")
        pl = QHBoxLayout(self.progress_row)
        pl.setContentsMargins(20, 6, 16, 6)
        self.progress = Spinner(16)
        self.progress_text = ElidedLabel("", Qt.ElideRight)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("rowAction")
        cancel.clicked.connect(self.cancel_ai)
        pl.addWidget(self.progress)
        pl.addWidget(self.progress_text, 1)
        pl.addWidget(cancel)
        self.progress_row.hide()
        return self.progress_row

    def _left(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("sidePanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)
        self.overview_btn = QPushButton("Overview and general discussion")
        self.overview_btn.setCheckable(True)
        self.overview_btn.clicked.connect(lambda: self.show_file(OVERVIEW))
        lay.addWidget(self.overview_btn)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter files  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.populate)
        lay.addWidget(self.search)
        # Show only one extension (display only: nothing is marked viewed).
        self.ext = ""
        self.ext_bar = ExtensionBar()
        self.ext_bar.toggled.connect(self.set_ext)
        lay.addWidget(self.ext_bar)
        row = QHBoxLayout()
        self.viewed_label = QLabel("")
        self.viewed_label.setObjectName("muted")
        row.addWidget(self.viewed_label, 1)
        self.hide_viewed = QPushButton("Hide viewed")
        self.hide_viewed.setObjectName("rowAction")
        self.hide_viewed.setCheckable(True)
        self.hide_viewed.toggled.connect(self.populate)
        row.addWidget(self.hide_viewed)
        lay.addLayout(row)
        self.view = FileView(self.main.config.file_tree)
        self.checked: set[str] = set()
        self.view.checks = self.checked
        self.view.dim_checked = True
        self.view.check_toggled.connect(lambda files, on: self.set_viewed([f.path for f in files], on))
        self.view.activated.connect(self._space)
        self.view.selection_changed.connect(self._on_select)
        lay.addWidget(self.view, 1)
        hint = QLabel("Tick = viewed · Space: viewed and next · click a line number to comment")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return panel

    def _overview_page(self) -> QWidget:
        self.overview = QScrollArea()
        self.overview.setObjectName("plainScroll")
        self.overview.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("overviewPage")
        holder.setStyleSheet(f"QWidget#overviewPage {{ background: {C['bg']}; }}")
        outer = QHBoxLayout(holder)
        outer.setContentsMargins(24, 18, 24, 24)
        column = QWidget()
        self.overview_lay = QVBoxLayout(column)
        self.overview_lay.setContentsMargins(0, 0, 0, 0)
        self.overview_lay.setSpacing(10)
        self.overview_lay.addStretch()
        outer.addWidget(column, 1)
        self.overview.setWidget(holder)
        return self.overview

    # ---------- loading ----------
    def load(self):
        if self.busy:
            return
        self.busy = True
        self.sub.setText("Loading the merge request…")
        self.loading_bar.show()
        if not self.files:
            self.diff.set_message("Loading the merge request…")
        mr = self.mr

        def work():
            client = gitlab.Client.for_project(mr.host, mr.project)
            fresh = client.merge_request(mr.project, mr.iid)
            fresh.reasons = mr.reasons
            files = client.diffs(mr.project, mr.iid)
            discussions = client.discussions(mr.project, mr.iid)
            try:
                drafts = client.draft_count(mr.project, mr.iid)
            except GitError:
                drafts = 0
            return client, fresh, files, discussions, drafts

        self.tasks.submit_api(work, self._on_load)

    def _on_load(self, result, error):
        self.busy = False
        self.loading_bar.hide()
        if error:
            self.sub.setText(str(error))
            self.diff.set_message(str(error))
            return
        self.client, self.mr, self.files, self.discussions, self.remote_drafts = result
        self.by_path = {f.path: f for f in self.files}
        self.parsed.clear()
        # A file viewed before a push that changed it is unviewed again.
        self.viewed = {p: sig for p, sig in self.viewed.items() if p in self.by_path
                       and self.by_path[p].signature == sig}
        self.checked.clear()
        self.checked.update(self.viewed)
        mr = self.mr
        self.title.setText(f"!{mr.iid}  {mr.title}")
        self.setWindowTitle(f"!{mr.iid} · {mr.title}")
        bits = [mr.project, f"by {mr.author}", f"{mr.source_branch} → {mr.target_branch}",
                f"{len(self.files)} files"]
        if mr.draft:
            bits.append("Draft")
        if mr.conflicts:
            bits.append("Has conflicts")
        self.sub.setText("  ·  ".join(bits))
        self.render_status()
        self.populate()
        if not self.current:
            first = next((f.path for f in sorted(self.files, key=lambda f: f.path.lower())
                          if f.path not in self.checked), None)
            self.show_file(first or OVERVIEW)
        else:
            self.show_file(self.current, keep=True)
        self.update_counts()

    def save_state(self):
        state = gitlab.load_state()
        state[self.mr.key] = {"viewed": self.viewed, "pending": self.pending, "summary": self.summary,
                              "saved": time.time()}
        gitlab.save_state(state)

    # ---------- file list ----------
    def _change(self, f: gitlab.FileDiff) -> FileChange:
        code = "A" if f.new_file else "D" if f.deleted else "R" if f.renamed else "M"
        return FileChange(f.path, code, False, f.old_path if f.renamed else "")

    def populate(self):
        term = self.search.text().strip().lower()
        hide = self.hide_viewed.isChecked()
        # Hidden when viewed, unless something there still needs attention (it stays, greyed out).
        busy = self._commented() if hide else set()
        shown = [self._change(f) for f in self.files
                 if (not term or term in f.path.lower()) and not (hide and f.path in self.checked
                                                                   and f.path not in busy)
                 and (not self.ext or extension(f.path) == self.ext)]
        self.ext_bar.set_files([self._change(f) for f in self.files], self.ext)
        self.view.badges = self._badges()
        keep = (False, self.current) if self.current in self.by_path else ("", "")
        self.view.set_files(sorted(shown, key=lambda c: c.path.lower()), keep)
        self.update_counts()

    def set_ext(self, ext: str):
        self.ext = ext
        self.populate()

    def _commented(self) -> set[str]:
        """Files with an open thread, a comment of mine not sent yet, or an AI proposal."""
        paths = {d.path for d in self.discussions if d.path and not d.resolved}
        return paths | {c["path"] for c in self.pending if c.get("path") not in (None, OVERVIEW)}

    def _badges(self) -> dict[str, tuple[str, str]]:
        counts: dict[str, list[int]] = {}
        for d in self.discussions:
            if d.path:
                counts.setdefault(d.path, [0, 0, 0])[0 if not d.resolved else 2] += 1
        for c in self.pending:
            if c.get("path") and c["path"] != OVERVIEW:
                counts.setdefault(c["path"], [0, 0, 0])[1] += 1
        out = {}
        for path, (open_, mine, resolved) in counts.items():
            proposals = sum(1 for c in self.pending if c.get("path") == path and c["status"] == "proposed")
            if proposals:
                out[path] = (f"✨ {proposals}", C["violet"])
            elif mine:
                out[path] = (f"✎ {mine}", C["green"])
            elif open_:
                out[path] = (f"💬 {open_}", C["blue"])
            elif resolved:
                out[path] = (f"✓ {resolved}", C["faint"])
        return out

    def update_counts(self):
        n = len(self.files)
        self.viewed_label.setText(f"{len(self.checked)} / {n} viewed" if n else "")
        mine = [c for c in self.pending if c["status"] == "pending"]
        proposals = [c for c in self.pending if c["status"] == "proposed"]
        extra = f" + {self.remote_drafts} drafts" if self.remote_drafts else ""
        self.send_btn.setText(f"Send {len(mine)} comment{'s' if len(mine) != 1 else ''}{extra}" if mine
                              else "Send comments")
        self.send_btn.setEnabled(bool(mine or self.remote_drafts) and not self.busy)
        self.ai_left.setText(f"{len(proposals)} AI proposal{'s' if len(proposals) != 1 else ''} to go through"
                             if proposals else "")
        self.ai_next.setVisible(bool(proposals))
        general = sum(1 for d in self.discussions if not d.path and not d.resolved)
        self.overview_btn.setText("Overview and general discussion" + (f"  ·  💬 {general}" if general else ""))

    def set_viewed(self, paths: list[str], on: bool):
        for p in paths:
            if on and p in self.by_path:
                self.viewed[p] = self.by_path[p].signature
                self.checked.add(p)
            else:
                self.viewed.pop(p, None)
                self.checked.discard(p)
        self.view.viewport().update()
        self.update_counts()
        self.save_state()
        if self.hide_viewed.isChecked():
            QTimer.singleShot(0, self.populate)  # after the click that ticked it, not during

    def _space(self, files):
        """Space / Enter / double-click: mark viewed and open the next file not viewed yet."""
        paths = [f.path for f in files]
        if not paths:
            return
        self.set_viewed(paths, True)
        order = [f.path for f in sorted(self.files, key=lambda f: f.path.lower())]
        after = order[order.index(paths[-1]) + 1:] if paths[-1] in order else order
        nxt = next((p for p in after if p not in self.checked), None)
        if nxt:
            self.show_file(nxt)
        if self.hide_viewed.isChecked():
            self.populate()

    def _on_select(self):
        fc = self.view.current_file()
        if fc and fc.path != self.current:
            self.show_file(fc.path)

    # ---------- diff ----------
    def show_file(self, path: str, keep: bool = False):
        if not path:
            return
        self.current = path
        self.overview_btn.setChecked(path == OVERVIEW)
        self.file_comment.setVisible(path != OVERVIEW)
        if path == OVERVIEW:
            self.view.blockSignals(True)
            self.view.clearSelection()
            self.view.blockSignals(False)
            self.render_overview()
            self.center.setCurrentIndex(1)
            self.update_counts()
            return
        f = self.by_path.get(path)
        if f is None:
            return
        self.center.setCurrentIndex(0)
        if not self.view.current_file() or self.view.current_file().path != path:
            self.view.select_path(path)
        self.diff.set_inline(self._thread_widgets(path), render=False)
        if f.too_large and not f.diff:
            self.diff.set_message("GitLab does not send this diff (too large): loading it with git…", path)
            self._load_local_diff(f)
        else:
            data = self.parsed.get(path)
            if data is None:
                data = self.parsed[path] = parse_diff(f.diff)
            self.diff.set_diff(path, data, keep_scroll=keep)
        self._markers()
        self.update_counts()

    def _load_local_diff(self, f: gitlab.FileDiff):
        mr, cred = self.mr, self._cred()

        def work():
            repo = ai_review.cache_repo(mr.host, mr.project, cred)
            try:
                run_git(["cat-file", "-e", f"{mr.head_sha}^{{commit}}"], cwd=repo, timeout=15)
            except GitError:
                run_git(["fetch", "--no-tags", "origin", f"refs/merge-requests/{mr.iid}/head"], cwd=repo,
                        cred=cred, timeout=300)
            paths = [f.old_path, f.new_path] if f.renamed else [f.path]
            return run_git(["diff", "--no-color", "--no-ext-diff", "-M", mr.base_sha, mr.head_sha, "--", *paths],
                           cwd=repo, timeout=120)

        def done(text, error):
            if error:
                if self.current == f.path:
                    self.diff.set_message(f"Could not get the diff with git: {error}", f.path)
                return
            f.diff, f.too_large = text, False
            self.parsed.pop(f.path, None)
            if self.current == f.path:
                self.show_file(f.path)

        self.tasks.submit_api(work, done)

    def _markers(self):
        marks: dict[tuple[str, int], tuple[int, str]] = {}

        def add(side, line, color):
            if line:
                count = marks.get((side, line), (0, color))[0]
                marks[(side, line)] = (count + 1, color)

        for d in self.discussions:
            if d.path == self.current:
                add("new" if d.new_line else "old", d.new_line or d.old_line, C["faint"] if d.resolved else C["blue"])
        for c in self.pending:
            if c.get("path") == self.current and c.get("line"):
                add(c["side"], c["line"], C["violet"] if c["status"] == "proposed" else C["green"])
        self.diff.set_markers(marks)

    # ---------- comments ----------
    def _park_focus(self):
        """Move the focus out of the cards about to be rebuilt: when a focused widget disappears, Qt gives
        the focus to the next one and scrolls to it (the bottom of the page, or the end of the diff)."""
        focused = QApplication.focusWidget()
        if focused is not None and self.center.isAncestorOf(focused):
            self.view.setFocus(Qt.OtherFocusReason)

    def render_cards(self):
        """Redraw the comments of what is shown (inline in the diff, or the overview page)."""
        self._park_focus()
        if self.current == OVERVIEW:
            self.render_overview()
        elif self.current in self.by_path:
            self.diff.set_inline(self._thread_widgets(self.current))
            self._markers()
        self.update_counts()

    def _items(self, path: str) -> list[tuple[str | tuple, int, QWidget]]:
        """(anchor key, sort line, card) of every thread, pending comment and proposal of a file."""
        items = []
        if self.composer and self.composer[0] == path:
            _p, side, line = self.composer
            items.append(((side, line) if line else "top", line or 0, self._composer_card()))
        for d in self.discussions:
            if (d.path or OVERVIEW) == path:
                side = "new" if d.new_line else "old"
                line = d.new_line or d.old_line
                items.append(((side, line) if line else "top", line or 0, self._discussion_card(d)))
        for c in self.pending:
            if c.get("path", OVERVIEW) == path:
                key = (c["side"], c["line"]) if c.get("line") else "top"
                items.append((key, c.get("line") or 0, self._pending_card(c)))
        return items

    def _thread_widgets(self, path: str) -> dict:
        """One stack of cards per commented line, shown under that line inside the diff."""
        groups: dict = {}
        for key, _line, card in self._items(path):
            groups.setdefault(key, []).append(card)
        out = {}
        for key, cards in groups.items():
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            for card in cards:
                lay.addWidget(card)
            out[key] = box
        return out

    def render_overview(self):
        self._park_focus()
        bar = self.overview.verticalScrollBar()
        scroll = bar.value()
        composing = bool(self.composer and self.composer[0] == OVERVIEW)

        def restore():
            bar.setValue(scroll)  # once the rebuilt page is laid out
            if composing and self._composer_widget is not None:
                self.overview.ensureWidgetVisible(self._composer_widget, 0, 40)  # the new comment box

        QTimer.singleShot(0, restore)
        lay = self.overview_lay
        while lay.count() > 1:
            w = lay.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        mr = self.mr
        widgets: list[QWidget] = []
        desc = Card("DESCRIPTION", f"{mr.author} · {mr.source_branch} → {mr.target_branch}", C["accent"])
        desc.lay.addWidget(_markdown(mr.description or "_No description._"))
        desc.finish()
        widgets.append(desc)
        if self.summary:
            s = Card("AI SUMMARY", "not sent", C["violet"])
            s.lay.addWidget(_markdown(self.summary))
            s.add_button("Add as comment", self._summary_as_comment)
            s.finish()
            widgets.append(s)
        widgets.append(self._heading("General discussion"))
        general = sorted(self._items(OVERVIEW), key=lambda it: it[1])
        widgets += [card for _k, _l, card in general]
        if not (self.composer and self.composer[0] == OVERVIEW):
            add = QPushButton("Write a general comment")
            add.setObjectName("rowAction")
            add.clicked.connect(lambda: self.start_comment(None))
            widgets.append(add)
        paths = sorted({d.path for d in self.discussions if d.path} |
                       {c["path"] for c in self.pending if c.get("path") not in (None, OVERVIEW)}, key=str.lower)
        widgets.append(self._heading(f"Comments in files ({len(paths)} file{'s' if len(paths) != 1 else ''})"
                                     if paths else "No comment in the files yet"))
        for path in paths:
            head = QPushButton(path)
            head.setFlat(True)
            head.setCursor(Qt.PointingHandCursor)
            head.setToolTip("Open this file")
            head.setStyleSheet(f"text-align: left; color: {C['text']}; font-weight: 700; border: none; "
                               "background: transparent; padding: 6px 0 0 0;")
            head.clicked.connect(lambda _=False, p=path: self.show_file(p))
            widgets.append(head)
            for key, _line, card in sorted(self._items(path), key=lambda it: it[1]):
                if isinstance(card, Card):
                    side, line = key if isinstance(key, tuple) else (None, None)
                    go = QPushButton("Go to file ▸")
                    go.setObjectName("rowAction")
                    go.setToolTip(f"Open {path}" + (f" at line {line}" if line else ""))
                    go.clicked.connect(lambda _=False, p=path, s=side, n=line: self._jump(s, n, p))
                    card.buttons.insertWidget(0, go)  # left, apart from the card's own actions
                widgets.append(card)
        for w in widgets:
            lay.insertWidget(lay.count() - 1, w)

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size: 11pt; font-weight: 700; margin-top: 10px;")
        return label

    def _where(self, path: str, side: str | None, line: int | None) -> str:
        if not line:
            return "general" if path == OVERVIEW else "whole file"
        return f"line {line}" + (" (removed)" if side == "old" else "")

    def _jump(self, side, line, path: str = ""):
        """Show a line of a file (from the overview, open that file first)."""
        if path and path != OVERVIEW and path != self.current:
            self.show_file(path)
            if line:
                QTimer.singleShot(0, lambda: self.diff.scroll_to(side or "new", line))
            return
        if line and self.current != OVERVIEW:
            self.diff.scroll_to(side or "new", line)

    def _composer_card(self) -> QWidget:
        path, side, line = self.composer
        card = Card("NEW COMMENT", self._where(path, side, line), C["green"], lambda: self._jump(side, line, path))
        card.lay.addWidget(Editor("", "Add to review", self._add_comment, self._cancel_comment))
        self._composer_widget = card
        return card

    def start_comment(self, line):
        if self.current == "" or self.client is None:
            return
        if line is None:
            self.composer = (self.current, None, None)
        else:
            key = DiffView.line_key(line)
            if key is None:
                return
            self.composer = (self.current, key[0], key[1])
        self.render_cards()
        if line is None and self.current != OVERVIEW:
            # A whole-file comment opens above the first line: bring it into view.
            QTimer.singleShot(0, lambda: [e.verticalScrollBar().setValue(0) for e in self.diff.editors()])

    def _add_comment(self, text: str):
        path, side, line = self.composer
        self.pending.append({"id": uuid.uuid4().hex, "path": path, "side": side, "line": line, "body": text,
                             "status": "pending", "source": "me"})
        self.composer = None
        self.save_state()
        self.populate()
        self.render_cards()

    def _cancel_comment(self):
        self.composer = None
        self.render_cards()

    def _discussion_card(self, d: gitlab.Discussion) -> QWidget:
        side = "new" if d.new_line else "old"
        state = " · resolved" if d.resolved else ""
        card = Card("THREAD" if d.resolvable else "COMMENT", self._where(d.path or OVERVIEW, side,
                    d.new_line or d.old_line) + state, C["faint"] if d.resolved else C["blue"],
                    lambda: self._jump(side, d.new_line or d.old_line, d.path))
        notes = d.notes if not d.resolved or len(d.notes) <= 2 else d.notes[:1] + d.notes[-1:]
        for i, n in enumerate(notes):
            who = QLabel(f"<b>{n.author}</b> <span style='color:{C['faint']}'>· {when(n.created)}</span>")
            who.setTextFormat(Qt.RichText)
            if i:
                who.setStyleSheet("margin-top: 4px;")
            card.lay.addWidget(who)
            card.lay.addWidget(_markdown(n.body))
        if self.replying == d.id:
            card.lay.addWidget(Editor("", "Reply", lambda t, d=d: self.reply(d, t), self._cancel_reply,
                                      "Reply (sent right away)"))
        else:
            card.add_button("Reply", lambda: self._open_reply(d))
            if d.resolvable:
                card.add_button("Unresolve" if d.resolved else "Resolve", lambda: self.resolve(d, not d.resolved))
        card.finish()
        return card

    def _pending_card(self, c: dict) -> QWidget:
        proposed = c["status"] == "proposed"
        severity = c.get("severity", "")
        color = SEVERITY_COLORS.get(severity, C["violet"]) if proposed else C["green"]
        kind = (f"✨ AI · {severity.upper()}" if proposed else
                "✎ PENDING" + (" · FROM AI" if c.get("source") == "ai" else ""))
        card = Card(kind, self._where(c.get("path", OVERVIEW), c.get("side"), c.get("line")), color,
                    lambda: self._jump(c.get("side"), c.get("line"), c.get("path", "")))
        if self.editing == c["id"]:
            card.lay.addWidget(Editor(c["body"], "Accept" if proposed else "Save",
                                      lambda t, c=c: self._save_edit(c, t), self._cancel_edit))
        else:
            card.lay.addWidget(_markdown(c["body"]))
            if proposed:
                card.add_button("Dismiss", lambda: self._remove(c), danger=True)
                card.add_button("Edit", lambda: self._edit(c))
                card.add_button("Accept", lambda: self._accept(c), primary=True)
            else:
                card.add_button("Delete", lambda: self._remove(c), danger=True)
                card.add_button("Edit", lambda: self._edit(c))
        card.finish()
        return card

    def _edit(self, c):
        self.editing = c["id"]
        self.render_cards()

    def _cancel_edit(self):
        self.editing = ""
        self.render_cards()

    def _save_edit(self, c, text):
        c["body"], c["status"] = text, "pending"
        self.editing = ""
        self._changed()

    def _accept(self, c):
        c["status"] = "pending"
        self._changed()

    def _remove(self, c):
        self.pending = [x for x in self.pending if x["id"] != c["id"]]
        self._changed()

    def _changed(self):
        self.save_state()
        self.populate()
        self.render_cards()

    def _summary_as_comment(self):
        self.pending.append({"id": uuid.uuid4().hex, "path": OVERVIEW, "side": None, "line": None,
                             "body": self.summary, "status": "pending", "source": "ai"})
        self.summary = ""
        self._changed()

    def next_proposal(self):
        order = {p: i for i, p in enumerate(sorted(self.by_path, key=str.lower))}
        order[OVERVIEW] = -1
        items = sorted((c for c in self.pending if c["status"] == "proposed"),
                       key=lambda c: (order.get(c.get("path"), 10 ** 6), c.get("line") or 0))
        if not items:
            return
        here = order.get(self.current, -2)
        target = next((c for c in items if order.get(c.get("path"), 0) > here), items[0])
        self.show_file(target.get("path") or OVERVIEW)
        if target.get("line"):
            QTimer.singleShot(0, lambda: self._jump(target["side"], target["line"]))

    # ---------- discussions (sent right away) ----------
    def _open_reply(self, d):
        self.replying = d.id
        self.render_cards()

    def _cancel_reply(self):
        self.replying = ""
        self.render_cards()

    def reply(self, d: gitlab.Discussion, text: str):
        self.replying = ""
        self._write(lambda: self.client.reply(self.mr.project, self.mr.iid, d.id, text), "Reply")

    def resolve(self, d: gitlab.Discussion, resolved: bool):
        self._write(lambda: self.client.resolve(self.mr.project, self.mr.iid, d.id, resolved),
                    "Resolve" if resolved else "Unresolve")

    def _write(self, fn, label: str):
        self.sub.setText(f"{label}…")

        def work():
            fn()
            return self.client.discussions(self.mr.project, self.mr.iid)

        def done(discussions, error):
            if error:
                QMessageBox.warning(self, label, str(error))
                self.sub.setText("")
                self.render_cards()
                return
            self.discussions = discussions
            self.sub.setText(f"{label}: done")
            self.populate()
            self.render_cards()

        self.tasks.submit_api(work, done)

    # ---------- send the review ----------
    def send(self):
        mine = [c for c in self.pending if c["status"] == "pending"]
        if self.busy or self.client is None or not (mine or self.remote_drafts):
            return
        proposals = sum(1 for c in self.pending if c["status"] == "proposed")
        text = (f"Send {len(mine)} comment{'s' if len(mine) != 1 else ''} to GitLab as one review?"
                + (f"\n\n{self.remote_drafts} draft comment(s) already pending on GitLab (written in the browser) "
                   "are published with them." if self.remote_drafts else "")
                + (f"\n\n{proposals} AI proposal(s) not accepted yet stay here, unsent." if proposals else ""))
        if QMessageBox.question(self, "Send comments", text) != QMessageBox.Yes:
            return
        self.busy = True
        self.update_counts()
        self.sub.setText("Sending comments…")
        mr, client, by_path = self.mr, self.client, dict(self.by_path)

        def work():
            sent, moved = [], 0
            try:
                for c in mine:
                    f = by_path.get(c.get("path"))
                    line, side = c.get("line"), c.get("side")
                    new_line = old_line = None
                    if f is not None and line:
                        ctx = next((l for l in parse_diff(f.diff).lines if l.kind == "ctx" and (
                            (side == "new" and l.new == line) or (side == "old" and l.old == line))), None)
                        if ctx is not None:  # GitLab needs both numbers on an unchanged line.
                            new_line, old_line = ctx.new, ctx.old
                        elif side == "new":
                            new_line = line
                        else:
                            old_line = line
                    try:
                        client.add_draft(mr, c["body"], f, new_line, old_line)
                    except gitlab.GitLabError:
                        if f is None or not line:
                            raise
                        # Position refused (diff changed since): keep the comment, as a general one.
                        client.add_draft(mr, f"**`{f.path}` line {line}**\n\n{c['body']}")
                        moved += 1
                    sent.append(c["id"])
                client.publish_drafts(mr)
            except GitError as exc:
                return sent, moved, exc
            return sent, moved, None

        def done(result, error):
            self.busy = False
            sent, moved, failure = result if result else ([], 0, error)
            self.pending = [c for c in self.pending if c["id"] not in set(sent)]
            self.save_state()
            if failure or error:
                QMessageBox.warning(self, "Send comments",
                                    f"{len(sent)} comment(s) reached GitLab as drafts before the error:\n\n"
                                    f"{failure or error}\n\nThey are published with the next Send, "
                                    "or from GitLab's “Submit review”.")
            else:
                note = f" ({moved} placed as general comments: their line changed)" if moved else ""
                self.sub.setText(f"Review sent: {len(sent)} comment(s){note}")
            self.load()

        self.tasks.submit_api(work, done)

    # ---------- AI review ----------
    def start_ai(self):
        if self.ai_run is not None or self.client is None or not self.files:
            return
        # A new run replaces the last one: its proposals not handled yet and its summary go. Accepted or
        # edited ones stay, they are the user's comments now.
        self.pending = [c for c in self.pending if c["status"] != "proposed"]
        self.summary = ""
        self.save_state()
        self.populate()
        self.render_cards()
        cfg = self.main.config
        self.ai_run = ai_review.Run()
        self.ai_started = time.monotonic()
        self.ai_btn.setEnabled(False)
        self.progress_row.show()
        self.progress_text.setText("Checking Claude Code…")
        mr, files, discussions, run = self.mr, list(self.files), list(self.discussions), self.ai_run
        client = self.client
        cred = self._cred()
        bridge = self.bridge

        def work():
            st = ai.status()
            if not st.ready:
                raise ai.AIError(st.detail + ("" if st.path else ". Install it from https://claude.com/claude-code")
                                 + (". Sign in with `claude auth login`." if st.path else ""))
            checkout = repo = None
            try:
                bridge.progress.emit(None, 0, "Updating GitEnough's own copy of the project (slow only the "
                                              "first time)…")
                repo = ai_review.cache_repo(mr.host, mr.project, cred)
                bridge.progress.emit(None, 0, "Checking out the merge request's commit for context…")
                checkout = ai_review.prepare_checkout(repo, mr, cred)
            except GitError as exc:
                bridge.progress.emit(None, 0, f"No checkout ({str(exc)[:80]}): reviewing the diff only")
            self.checkout = checkout
            try:
                if checkout:
                    guidelines = ai_review.local_guidelines(checkout, files)
                else:
                    found = []
                    for rel in ai_review.GUIDE_FILES:
                        try:
                            text = client.raw_file(mr.project, rel, mr.head_sha)
                        except GitError:
                            text = None
                        if text:
                            found.append((rel, text))
                    guidelines = ai_review.join_guidelines(found)
                if guidelines:
                    bridge.progress.emit(None, 0, "Project guidelines found in CLAUDE.md")
                skill = cfg.review_skill if cfg.review_skill and review_skill.installed_info(
                    cfg.review_skill) else ""
                if skill:
                    bridge.progress.emit(None, 0, f"Reviewing with the {skill} skill")
                return ai_review.review(mr, files, discussions, cfg.ai_review_model, guidelines,
                                        checkout, bridge.progress.emit, bridge.found.emit, run, skill)
            finally:
                if checkout:
                    ai_review.remove_checkout(repo, checkout)
                self.checkout = None

        self.tasks.submit_ai(work, self._on_ai_done)

    def _cred(self) -> Credential | None:
        """The host's API token, also used to fetch the merge request into the review cache."""
        key, token = vault.token_for(self.mr.host, self.mr.project)
        if not token:
            return None
        return Credential(self.main.config.hosts.get(key) or vault.default_username(key), token)

    def cancel_ai(self):
        if self.ai_run is not None:
            self.ai_run.cancel()
            self.progress_text.setText("Cancelling…")

    def _on_ai_progress(self, done, total, text):
        self.progress_text.setText(f"{text}   ·   {ai_review.elapsed(self.ai_started)}")

    def _on_ai_found(self, findings):
        for fnd in findings:
            f = self.by_path.get(fnd.file)
            attachable = ai_review.is_attachable(f, fnd.side, fnd.line)
            # The severity is part of the text: it reaches GitLab with the comment and can be edited.
            text = f"**{fnd.severity.capitalize()}** · {fnd.body.strip()}"
            body = text if attachable or f is None else f"(line {fnd.line}) {text}"
            self.pending.append({"id": uuid.uuid4().hex, "path": fnd.file if f else OVERVIEW,
                                 "side": fnd.side if attachable else None, "line": fnd.line if attachable else None,
                                 "body": body if f else f"**`{fnd.file}`**\n\n{text}",
                                 "status": "proposed", "source": "ai", "severity": fnd.severity})
        self.save_state()
        self.populate()
        if self.current:
            self.render_cards()

    def _on_ai_done(self, summary, error):
        run, self.ai_run = self.ai_run, None
        self.ai_btn.setEnabled(True)
        self.progress_row.hide()
        if error:
            if str(error) != "Cancelled":
                ai_error_dialog(self, str(error), "AI review")
            return
        if summary:
            self.summary = summary
        self.save_state()
        n = sum(1 for c in self.pending if c["status"] == "proposed")
        used = ", ".join(run.models) if run is not None and run.models else "Claude"
        self.sub.setText(f"AI review with {used} done in {ai_review.elapsed(self.ai_started)}: {n} proposal(s) "
                         "to go through")
        self.render_cards()
        if n:
            self.next_proposal()

    # ---------- pipeline, approvals, merge state ----------
    def render_status(self):
        mr = self.mr
        pipe = PIPELINE.get(mr.pipeline_status)
        self.pipeline_chip.setVisible(bool(mr.pipeline_status))
        if mr.pipeline_status:
            label, color = pipe or (f"Pipeline {mr.pipeline_status.replace('_', ' ')}", "faint")
            self.pipeline_chip.setText(label)
            self.pipeline_chip.setStyleSheet(chip_style(color))
        names = [n for n in mr.approved_by if n]
        if mr.approvals_required or names:
            done = len(names)
            text = f"✓  {done} / {mr.approvals_required} approvals" if mr.approvals_required else \
                f"✓  Approved by {', '.join(names)}" if len(names) <= 2 else f"✓  {done} approvals"
            ok = done >= mr.approvals_required and done > 0
            self.approvals_chip.setText(text)
            self.approvals_chip.setStyleSheet(chip_style("green" if ok else "orange" if mr.approvals_required else
                                                         "faint"))
            self.approvals_chip.setToolTip("Approved by " + ", ".join(names) if names else "No approval yet")
            self.approvals_chip.show()
        else:
            self.approvals_chip.hide()
        merge = MERGE.get(mr.merge_status)
        self.merge_chip.setVisible(bool(mr.merge_status))
        if mr.merge_status:
            label, color = merge or (mr.merge_status.replace("_", " ").capitalize(), "faint")
            self.merge_chip.setText(label)
            self.merge_chip.setStyleSheet(chip_style(color))
        # Approve is offered only when GitLab says so (not on your own merge request, rights, ...).
        self.approve_btn.setVisible(mr.can_approve or mr.user_approved)
        self.approve_btn.setText("Revoke approval" if mr.user_approved else "✓ Approve")
        self.approve_btn.setToolTip("Withdraw your approval" if mr.user_approved else
                                    "Approve the merge request in GitLab")
        self.approve_btn.setStyleSheet("" if mr.user_approved else
                                       f"QPushButton {{ color: {C['green']}; border-color: rgba(62,207,142,0.45); }}"
                                       "QPushButton:hover { background: rgba(62,207,142,0.12); }")
        if mr.pipeline_status in PIPELINE_ACTIVE:
            if not self.status_timer.isActive():
                self.status_timer.start()
        else:
            self.status_timer.stop()

    def toggle_approve(self):
        if self.client is None or self.busy:
            return
        approve = not self.mr.user_approved
        self.approve_btn.setEnabled(False)
        self.sub.setText("Approving…" if approve else "Revoking the approval…")
        mr, client = self.mr, self.client

        def work():
            client.approve(mr, approve)
            return client.merge_request(mr.project, mr.iid)

        def done(fresh, error):
            self.approve_btn.setEnabled(True)
            if error:
                QMessageBox.warning(self, "Approve", str(error))
                self.sub.setText("")
                return
            self._take_status(fresh)
            self.sub.setText("Approved" if approve else "Approval revoked")

        self.tasks.submit_api(work, done)

    def show_pipeline(self):
        if self.client is None or not self.mr.pipeline_id:
            return
        self.current = PIPELINE_VIEW
        self.overview_btn.setChecked(False)
        self.file_comment.hide()
        self.view.blockSignals(True)
        self.view.clearSelection()
        self.view.blockSignals(False)
        self.center.setCurrentIndex(2)
        self.pipeline_page.show_pipeline(self.client, self.mr.project, self.mr.pipeline_id, self.mr.pipeline_url)

    def refresh_status(self):
        """Pipeline and approvals only (while the pipeline runs): the diffs are not reloaded."""
        if self.client is None or self.busy or not self.isVisible():
            return
        if self.current == PIPELINE_VIEW:
            self.pipeline_page.reload()
        mr, client = self.mr, self.client
        self.tasks.submit_api(lambda: client.merge_request(mr.project, mr.iid),
                              lambda fresh, error: fresh is not None and not error and self._take_status(fresh))

    def _take_status(self, fresh: gitlab.MergeRequest):
        mr = self.mr
        for name in ("pipeline_status", "pipeline_url", "pipeline_id", "merge_status", "approvals_required",
                     "approved_by",
                     "user_approved", "can_approve", "draft", "conflicts"):
            setattr(mr, name, getattr(fresh, name))
        if fresh.head_sha and fresh.head_sha != mr.head_sha:
            self.sub.setText("New commits were pushed: press F5 to review the new version")
        self.render_status()

    def closeEvent(self, event):
        if self.ai_run is not None:
            if QMessageBox.question(self, "AI review", "The AI review is still running. Stop it and close?") \
                    != QMessageBox.Yes:
                event.ignore()
                return
            self.ai_run.cancel()
        self.save_state()
        super().closeEvent(event)
