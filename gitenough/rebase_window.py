"""Rebase --onto assistant: pick the branch, the new base and the old base commit; resolve; force push with lease."""

import time

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox, QPushButton, QStackedWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from . import git_ops, rebase
from .errors import explain
from .style import C
from .tree_window import rel_time
from .widgets import ElidedLabel, ErrorDialog, icon_button


def _box(text: str, color: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.RichText)
    q = QColor(color)
    label.setStyleSheet(f"background: rgba({q.red()},{q.green()},{q.blue()},0.10); border: 1px solid "
                        f"rgba({q.red()},{q.green()},{q.blue()},0.45); border-radius: 8px; padding: 9px 12px;")
    return label


class RebaseWindow(QWidget):
    def __init__(self, main, project, branch: str | None = None):
        super().__init__(main, Qt.Window)
        self.main, self.p, self.tasks = main, project, main.tasks
        self.path = project.path
        self.setup: rebase.RebaseSetup | None = None
        self.moving: list = []
        self.lease = ""  # remote value captured before rebasing
        self.branch = ""
        self.onto_label = ""
        self.push_after = True
        self.sides = ("", "")
        self.busy = False
        self.setWindowTitle(f"Rebase onto · {project.title or project.name}")
        self.resize(1180, 820)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 14, 16, 14)
        title = QLabel(project.title or project.name)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        chip = QLabel("Rebase onto")
        chip.setObjectName("branchChip")
        hl.addWidget(title)
        hl.addWidget(chip)
        self.status = ElidedLabel("", Qt.ElideRight)
        self.status.setObjectName("muted")
        self.status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hl.addWidget(self.status, 1)
        bash = icon_button("terminal", "Open Git Bash here")
        bash.clicked.connect(lambda: main.open_bash(self.path))
        hl.addWidget(bash)
        root.addWidget(header)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._setup_page(branch))
        self.pages.addWidget(self._conflict_page())
        self.pages.addWidget(self._done_page())
        root.addWidget(self.pages, 1)

        state = rebase.progress(self.path)
        if state.active:
            # A rebase is already stopped (started here earlier or from a terminal): go straight to it.
            self.push_after = False
            self.show_conflicts(state)
        else:
            self.reload_setup()

    # ---------- page 1: setup ----------
    def _setup_page(self, branch: str | None) -> QWidget:
        page = QWidget()
        page.setObjectName("sidePanel")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)
        row = QHBoxLayout()
        row.addWidget(QLabel("Rebase"))
        self.branch_box = QComboBox()
        self.branch_box.setMinimumWidth(260)
        snap = self.p.snap
        on_origin = snap.on_origin if snap else set()
        current = snap.status.branch if snap and snap.status else None
        locals_ = git_ops.local_branches(self.path)
        self.branch_box.addItems(locals_)
        self.branch_box.setCurrentText(branch or current or "")
        row.addWidget(self.branch_box)
        row.addWidget(QLabel("onto"))
        self.onto_box = QComboBox()
        self.onto_box.setEditable(True)
        self.onto_box.setMinimumWidth(240)
        base = snap.base if snap else ""
        ontos = ([f"origin/{base}"] if base in on_origin else []) + ([base] if base else [])
        ontos += [f"origin/{b}" for b in sorted(on_origin) if b != base] + [b for b in locals_ if b != base]
        self.onto_box.addItems(list(dict.fromkeys(ontos)))
        row.addWidget(self.onto_box)
        self.fetch_first = QCheckBox("Fetch first")
        self.fetch_first.setChecked(True)
        self.fetch_first.setToolTip("Update the remote branches before looking at them (recommended)")
        row.addWidget(self.fetch_first)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("rowAction")
        refresh.clicked.connect(self.reload_setup)
        row.addWidget(refresh)
        row.addStretch()
        lay.addLayout(row)
        self.branch_box.activated.connect(lambda *_: self.reload_setup())
        self.onto_box.activated.connect(lambda *_: self.reload_setup())

        self.warnings = QVBoxLayout()
        self.warnings.setSpacing(6)
        lay.addLayout(self.warnings)

        lay.addWidget(QLabel("<b>Old base</b>: click the last commit that is <b>not</b> part of your work. "
                             "Everything above it is replayed onto the new base."))
        self.commits = QTreeWidget()
        self.commits.setObjectName("branchTree")
        self.commits.setHeaderLabels(["", "Commit", "Message", "Author", "Date"])
        self.commits.setRootIsDecorated(False)
        self.commits.setUniformRowHeights(True)
        self.commits.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.commits.header()
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        for col, w in ((0, 110), (1, 90), (3, 150), (4, 110)):
            self.commits.setColumnWidth(col, w)
        self.commits.itemSelectionChanged.connect(self.update_preview)
        lay.addWidget(self.commits, 1)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.RichText)
        lay.addWidget(self.preview)
        opts = QHBoxLayout()
        self.autostash = QCheckBox("Stash my uncommitted changes during the rebase")
        self.autostash.setChecked(True)
        self.push_box = QCheckBox("Force push with lease when done")
        self.push_box.setChecked(True)
        self.push_box.setToolTip("git push --force-with-lease, checked against the remote value read before the "
                                 "rebase: refused if someone pushed in the meantime")
        opts.addWidget(self.autostash)
        opts.addWidget(self.push_box)
        opts.addStretch()
        self.start_btn = QPushButton("Rebase")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start)
        opts.addWidget(self.start_btn)
        lay.addLayout(opts)
        return page

    def reload_setup(self):
        branch, onto = self.branch_box.currentText().strip(), self.onto_box.currentText().strip()
        if not branch or not onto:
            return
        self.status.setText("Fetching…" if self.fetch_first.isChecked() else "Loading…")
        self.start_btn.setEnabled(False)
        fetch = self.fetch_first.isChecked() and bool(self.p.snap and self.p.snap.origin)
        cred = self.main.cred(self.p)

        def work():
            if fetch:
                git_ops.run_git(["fetch", "--prune"], cwd=self.path, cred=cred, timeout=180)
            return rebase.prepare(self.path, branch, onto)

        (self.tasks.submit_network if fetch else self.tasks.submit)(work, self._on_setup)

    def _on_setup(self, setup, error):
        self.status.setText("")
        if error:
            ErrorDialog(explain(str(error), "Prepare the rebase"), self.p.name, self).exec()
            return
        self.setup = setup
        while self.warnings.count():
            self.warnings.takeAt(0).widget().deleteLater()
        for w in setup.warnings:
            self.warnings.addWidget(_box(f"⚠  {w}", C["orange"]))
        self.autostash.setVisible(bool(setup.changes))
        self.push_box.setText("Force push with lease when done" if setup.upstream else "Push (publish) when done")
        self.commits.clear()
        default = None
        for c in setup.history:
            item = QTreeWidgetItem(["", c.sha[:8], c.subject, c.author, rel_time(c.time)])
            item.setData(0, Qt.UserRole, c.sha)
            item.setToolTip(2, c.subject)
            refs = [r.removeprefix("HEAD -> ") for r in c.refs if r != "HEAD"]
            if refs:
                item.setText(2, f"{c.subject}    [{', '.join(refs)}]")
            self.commits.addTopLevelItem(item)
            if c.sha == setup.merge_base:
                default = item
        self.commits.setCurrentItem(default or self.commits.topLevelItem(self.commits.topLevelItemCount() - 1))
        self.update_preview()

    def update_preview(self):
        items = self.commits.selectedItems()
        if not items or not self.setup:
            self.start_btn.setEnabled(False)
            return
        chosen = self.commits.indexOfTopLevelItem(items[0])
        for i in range(self.commits.topLevelItemCount()):
            item = self.commits.topLevelItem(i)
            if i < chosen:
                item.setText(0, "↻ moves")
                color = C["green"]
            elif i == chosen:
                item.setText(0, "● old base")
                color = C["orange"]
            else:
                item.setText(0, "")
                color = C["faint"]
            for col in range(5):
                item.setForeground(col, QColor(color if col < 2 else (C["text"] if i < chosen else C["faint"])))
        self.moving = self.setup.history[:chosen]
        n = len(self.moving)
        onto = self.onto_box.currentText().strip()
        self.preview.setText(f"<b>{n} commit{'s' if n != 1 else ''}</b> of <b>{self.setup.branch}</b> will be "
                             f"replayed onto <b>{onto}</b>. Equivalent to: "
                             f"<code>git rebase --onto {onto} {items[0].data(0, Qt.UserRole)[:10]} "
                             f"{self.setup.branch}</code>")
        self.start_btn.setEnabled(n > 0 and not self.busy)

    def start(self):
        items = self.commits.selectedItems()
        if not items or not self.setup or self.busy:
            return
        if self.setup.behind_upstream and QMessageBox.question(
                self, "Branch not up to date",
                f"{self.setup.branch} is {self.setup.behind_upstream} commit(s) behind {self.setup.upstream}.\n\n"
                "Rebasing and force-pushing now would drop those remote commits. Continue anyway?") \
                != QMessageBox.Yes:
            return
        self.branch, self.onto_label = self.setup.branch, self.onto_box.currentText().strip()
        self.lease, self.push_after = self.setup.remote_sha, self.push_box.isChecked()
        old_base = items[0].data(0, Qt.UserRole)
        self.run(rebase.start, "Rebasing", self.path, self.onto_label, old_base, self.branch,
                 self.autostash.isChecked() and bool(self.setup.changes))

    # ---------- page 2: conflicts ----------
    def _conflict_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("sidePanel")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)
        self.stop_info = QLabel("")
        self.stop_info.setTextFormat(Qt.RichText)
        self.stop_info.setWordWrap(True)
        lay.addWidget(self.stop_info)
        self.files = QListWidget()
        self.files.setObjectName("files")
        self.files.itemDoubleClicked.connect(lambda item: self.resolve(item.data(Qt.UserRole)))
        lay.addWidget(self.files, 1)
        per_file = QHBoxLayout()
        self.resolve_btn = QPushButton("Resolve in merge tool…")
        self.resolve_btn.setObjectName("primary")
        self.resolve_btn.clicked.connect(lambda: self._with_file(self.resolve))
        self.keep_left = QPushButton("Keep …")
        self.keep_left.clicked.connect(lambda: self._with_file(lambda f: self.keep(f, "ours")))
        self.keep_right = QPushButton("Keep …")
        self.keep_right.clicked.connect(lambda: self._with_file(lambda f: self.keep(f, "theirs")))
        mark = QPushButton("Mark as resolved")
        mark.setToolTip("You fixed the file yourself (IDE, editor): stage it")
        mark.clicked.connect(lambda: self._with_file(self.mark))
        for b in (self.resolve_btn, self.keep_left, self.keep_right, mark):
            per_file.addWidget(b)
        per_file.addStretch()
        lay.addLayout(per_file)
        actions = QHBoxLayout()
        abort = QPushButton("Abort rebase…")
        abort.setObjectName("danger")
        abort.clicked.connect(self.abort)
        skip = QPushButton("Skip this commit")
        skip.setToolTip("Drop the commit being applied and go on with the next one")
        skip.clicked.connect(lambda: self.run(rebase.resume, "Skipping", self.path, "skip"))
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setObjectName("primary")
        self.continue_btn.clicked.connect(lambda: self.run(rebase.resume, "Continuing", self.path, "continue"))
        actions.addWidget(abort)
        actions.addStretch()
        actions.addWidget(skip)
        actions.addWidget(self.continue_btn)
        lay.addLayout(actions)
        return page

    def show_conflicts(self, state: rebase.Progress):
        self.pages.setCurrentIndex(1)
        left, right = rebase.conflict_sides(self.path)
        # Started here: use the name picked in the setup page rather than git's guess.
        self.sides = (self.onto_label or left, self.branch or right)
        base_name, branch_name = self.sides
        n = len(state.conflicts)
        self.stop_info.setText(
            f"<span style='font-size:12pt'><b>Stopped at commit {state.step}</b>: {state.subject}</span><br>"
            f"<span style='color:{C['muted']}'>{n} file{'s' if n != 1 else ''} changed both in "
            f"<b style='color:#5aa9ff'>{base_name}</b> and in <b style='color:#b392f0'>{branch_name}</b>. "
            "Resolve each one, then Continue.</span>")
        self.keep_left.setText(f"Keep {base_name} version")
        self.keep_right.setText(f"Keep {branch_name} version")
        self.files.clear()
        for f in state.conflicts:
            item = QListWidgetItem(f"⚠  {f.path}")
            item.setData(Qt.UserRole, f.path)
            item.setForeground(QColor(C["orange"]))
            self.files.addItem(item)
        for f in state.resolved:
            item = QListWidgetItem(f"✓  {f.path}")
            item.setData(Qt.UserRole, f.path)
            item.setForeground(QColor(C["green"]))
            self.files.addItem(item)
        if self.files.count():
            self.files.setCurrentRow(0)
        self.continue_btn.setEnabled(not state.conflicts)
        self.continue_btn.setToolTip("" if not state.conflicts else "Resolve every file first")
        self.status.setText(f"{n} conflict(s) left" if n else "All conflicts resolved: Continue")

    def _with_file(self, fn):
        item = self.files.currentItem()
        if item:
            fn(item.data(Qt.UserRole))

    def resolve(self, file: str):
        from .merge_tool import MergeToolWindow
        left, right = self.sides
        win = MergeToolWindow(self.main, self.p, file, left, right)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.resolved.connect(lambda _f: self.refresh_state())
        win.show()

    def keep(self, file: str, side: str):
        self.run(lambda: (rebase.take_side(self.path, file, side), rebase.progress(self.path))[1], "Resolving")

    def mark(self, file: str):
        if rebase.has_markers(self.path, file) and QMessageBox.question(
                self, "Conflict markers left", f"{file} still contains conflict markers. Mark it resolved anyway?") \
                != QMessageBox.Yes:
            return
        self.run(lambda: (rebase.mark_resolved(self.path, file), rebase.progress(self.path))[1], "Resolving")

    def abort(self):
        box = QMessageBox(QMessageBox.Warning, "Abort rebase",
                          "Stop the rebase and put the branch back exactly as it was before it started?",
                          QMessageBox.Cancel, self)
        confirm = box.addButton("Abort rebase", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is confirm:
            self.push_after = False
            self.run(rebase.resume, "Aborting", self.path, "abort", aborted=True)

    def refresh_state(self):
        self.run(rebase.progress, "Checking", self.path)

    # ---------- page 3: done ----------
    def _done_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("sidePanel")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(40, 40, 40, 30)
        lay.addStretch()
        self.done_title = QLabel("")
        self.done_title.setAlignment(Qt.AlignCenter)
        self.done_title.setStyleSheet("font-size: 18pt; font-weight: 800;")
        self.done_text = QLabel("")
        self.done_text.setAlignment(Qt.AlignCenter)
        self.done_text.setWordWrap(True)
        self.done_text.setTextFormat(Qt.RichText)
        lay.addWidget(self.done_title)
        lay.addWidget(self.done_text)
        row = QHBoxLayout()
        row.addStretch()
        self.push_now = QPushButton("Force push with lease")
        self.push_now.clicked.connect(lambda: self.push(self.branch_now()))
        history = QPushButton("Open history")
        history.clicked.connect(lambda: self.main.open_tree(self.p))
        close = QPushButton("Close")
        close.setObjectName("primary")
        close.clicked.connect(self.close)
        for b in (self.push_now, history, close):
            row.addWidget(b)
        row.addStretch()
        lay.addSpacing(16)
        lay.addLayout(row)
        lay.addStretch()
        return page

    def branch_now(self) -> str:
        return self.branch or git_ops.read_status(self.path).branch or ""

    def show_done(self, aborted: bool = False):
        self.pages.setCurrentIndex(2)
        if aborted:
            self.done_title.setText("Rebase aborted")
            self.done_text.setText("The branch is back exactly as it was before the rebase.")
            self.push_now.hide()
            return
        n = len(self.moving)
        what = f"{n} commit{'s' if n != 1 else ''} of <b>{self.branch}</b> now {'sit' if n != 1 else 'sits'} on <b>{self.onto_label}</b>." \
            if self.branch else f"<b>{self.branch_now()}</b> has been rebased."
        self.done_title.setText("✓  Rebased")
        self.done_text.setText(what)
        self.push_now.setVisible(not self.push_after)
        if self.push_after:
            self.push(self.branch)

    def push(self, branch: str):
        if not branch:
            return
        if self.p.mismatch:
            QMessageBox.warning(self, "Push", "Origin URL differs from the configured URL: fix the remote first.")
            return
        self.status.setText("Force pushing with lease…")
        cred = self.main.cred(self.p)

        def done(sha, err):
            self.status.setText("")
            self.main.refresh_project(self.p)
            if err:
                ErrorDialog(explain(str(err), "Force push with lease"), self.p.name, self).exec()
                self.push_now.show()
                return
            self.push_now.hide()
            self.done_text.setText(self.done_text.text() + f"<br><br>Pushed <b>{branch}</b> ({sha}) with "
                                   "--force-with-lease.")

        self.tasks.submit_network(rebase.push_with_lease, done, self.path, branch, self.lease, cred)

    # ---------- plumbing ----------
    def run(self, fn, label: str, *args, aborted: bool = False):
        if self.busy:
            return
        self.busy = True
        self.status.setText(f"{label}…")

        def done(state, err):
            self.busy = False
            self.status.setText("")
            self.main.refresh_project(self.p)
            if err:
                ErrorDialog(explain(str(err), label), self.p.name, self).exec()
                current = rebase.progress(self.path)
                if current.active:
                    self.show_conflicts(current)
                return
            if aborted:
                self.show_done(aborted=True)
            elif state is not None and state.active:
                self.show_conflicts(state)
            else:
                self.show_done()

        self.tasks.submit(fn, done, *args)

    def changeEvent(self, event):
        # Back from the IDE after fixing files by hand: re-read the conflict state.
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow() and not self.busy
                and self.pages.currentIndex() == 1):
            self.refresh_state()
        super().changeEvent(event)
