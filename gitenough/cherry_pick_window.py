"""Cherry-pick assistant: selected commits onto a destination branch, conflicts resolved in the app."""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from . import cherry_pick, rebase
from .errors import explain
from .git_ops import run_git
from .rebase_window import _box
from .style import C
from .widgets import ElidedLabel, ErrorDialog, icon_button, keep_size


class CherryPickWindow(QWidget):
    def __init__(self, main, project, shas: list[str] | None = None):
        super().__init__(main, Qt.Window)
        self.main, self.p, self.tasks = main, project, main.tasks
        self.path = project.path
        self.shas = list(shas or [])
        self.setup: cherry_pick.Setup | None = None
        self.busy = False
        self.sides = ("", "")
        self.setWindowTitle(f"Cherry-pick · {project.title or project.name}")
        self.resize(1000, 720)
        keep_size(self, self.main.config, "cherry_pick")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 14, 16, 14)
        title = QLabel(project.title or project.name)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        chip = QLabel("Cherry-pick")
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
        self.pages.addWidget(self._setup_page())
        self.pages.addWidget(self._conflict_page())
        self.pages.addWidget(self._done_page())
        root.addWidget(self.pages, 1)

        state = cherry_pick.progress(self.path)
        if state.active:
            self.show_conflicts(state)  # stopped earlier (here or in a terminal): straight to it
        elif self.shas:
            self.reload_setup()

    # ---------- page 1: what and where ----------
    def _setup_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("sidePanel")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(10)
        self.what = QLabel("")
        self.what.setStyleSheet("font-weight: 700; font-size: 11pt;")
        lay.addWidget(self.what)
        self.commits = QListWidget()
        self.commits.setObjectName("files")
        lay.addWidget(self.commits, 1)
        lay.addWidget(QLabel("Onto branch"))
        self.dest = QComboBox()
        self.dest.setEditable(True)  # type to filter a long branch list
        self.dest.setInsertPolicy(QComboBox.NoInsert)
        self.dest.currentTextChanged.connect(self.update_preview)
        lay.addWidget(self.dest)
        self.record = QCheckBox("Record the original commit in the message (cherry picked from commit …)")
        self.record.setChecked(True)
        lay.addWidget(self.record)
        self.come_back = QCheckBox("")
        self.come_back.setChecked(True)
        lay.addWidget(self.come_back)
        self.note = _box("", C["blue"])
        lay.addWidget(self.note)
        row = QHBoxLayout()
        row.addStretch()
        self.start_btn = QPushButton("Cherry-pick")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start)
        row.addWidget(self.start_btn)
        lay.addLayout(row)
        return page

    def reload_setup(self):
        self.status.setText("Reading the commits…")
        self.tasks.submit(cherry_pick.prepare, self._on_setup, self.path, self.shas)

    def _on_setup(self, setup, error):
        self.status.setText("")
        if error:
            ErrorDialog(explain(str(error), "Cherry-pick"), self.p.name, self).exec()
            return
        self.setup = setup
        n = len(setup.commits)
        self.what.setText(f"{n} commit{'s' if n != 1 else ''} to apply, oldest first")
        self.commits.clear()
        for c in setup.commits:
            item = QListWidgetItem(f"{c.sha[:8]}   {c.subject}   ·  {c.author}")
            self.commits.addItem(item)
        self.dest.blockSignals(True)
        self.dest.clear()
        for name in setup.local:
            self.dest.addItem(name)
        for name in setup.remote:
            self.dest.addItem(name)  # origin/x: a local branch tracking it is created
        self.dest.setCurrentIndex(max(0, self.dest.findText(setup.current)))  # pick another one to back-port
        self.dest.blockSignals(False)
        self.update_preview()

    def update_preview(self):
        s = self.setup
        if s is None:
            return
        dest = self.dest.currentText().strip()
        valid = dest in s.local or dest in s.remote
        self.start_btn.setEnabled(bool(valid and s.commits) and not self.busy)
        here = dest == s.current
        self.come_back.setText(f"Come back to {s.current} afterwards" if s.current else "Stay on the destination")
        self.come_back.setVisible(bool(s.current) and not here)
        bits = []
        if here:
            bits.append(f"Applied on <b>{dest}</b>, the branch you are on.")
        elif dest in s.remote:
            bits.append(f"A local branch <b>{dest.split('/', 1)[1]}</b> tracking <b>{dest}</b> is created, "
                        "and the commits are applied there.")
        elif valid:
            bits.append(f"GitEnough switches to <b>{dest}</b> and applies the commits there.")
        else:
            bits.append("Pick a branch from the list.")
        if s.dirty and not here and valid:
            bits.append(f"Your local changes on {s.current or 'this branch'} are put aside (stash) meanwhile, and "
                        "come back when you return.")
        bits.append("On a conflict, the cherry-pick stops and you resolve it here, as for a rebase.")
        self.note.setText("<br>".join(bits))

    def start(self):
        dest = self.dest.currentText().strip()
        if self.setup is None or self.busy:
            return
        self.go_back = self.come_back.isVisible() and self.come_back.isChecked()
        self.run(cherry_pick.start, "Cherry-picking", self.path, [c.sha for c in self.setup.commits], dest,
                 self.record.isChecked())

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
        resolve = QPushButton("Resolve in merge tool…")
        resolve.setObjectName("primary")
        resolve.clicked.connect(lambda: self._with_file(self.resolve))
        self.keep_left = QPushButton("Keep …")
        self.keep_left.clicked.connect(lambda: self._with_file(lambda f: self.keep(f, "ours")))
        self.keep_right = QPushButton("Keep …")
        self.keep_right.clicked.connect(lambda: self._with_file(lambda f: self.keep(f, "theirs")))
        mark = QPushButton("Mark as resolved")
        mark.setToolTip("You fixed the file yourself (IDE, editor): stage it")
        mark.clicked.connect(lambda: self._with_file(self.mark))
        for b in (resolve, self.keep_left, self.keep_right, mark):
            per_file.addWidget(b)
        per_file.addStretch()
        lay.addLayout(per_file)
        actions = QHBoxLayout()
        abort = QPushButton("Abort cherry-pick…")
        abort.setObjectName("danger")
        abort.clicked.connect(self.abort)
        self.skip_btn = QPushButton("Skip this commit")
        self.skip_btn.setToolTip("Leave this commit out and go on with the next one")
        self.skip_btn.clicked.connect(lambda: self.run(cherry_pick.resume, "Skipping", self.path, "skip"))
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setObjectName("primary")
        self.continue_btn.clicked.connect(lambda: self.run(cherry_pick.resume, "Continuing", self.path, "continue"))
        actions.addWidget(abort)
        actions.addStretch()
        actions.addWidget(self.skip_btn)
        actions.addWidget(self.continue_btn)
        lay.addLayout(actions)
        return page

    def show_conflicts(self, state: cherry_pick.Progress):
        self.pages.setCurrentIndex(1)
        # Sides named after branches: the destination, and the commit being applied.
        self.sides = rebase.conflict_sides(self.path)
        left, right = self.sides
        n = len(state.conflicts)
        step = f" {state.step}" if state.step else ""
        if state.empty:
            text = (f"<span style='color:{C['muted']}'>This commit brings nothing new to "
                    f"<b>{state.dest or left}</b>: its changes are already there. Skip it.</span>")
        else:
            text = (f"<span style='color:{C['muted']}'>{n} file{'s' if n != 1 else ''} changed both in "
                    f"<b style='color:#5aa9ff'>{left}</b> and in <b style='color:#b392f0'>{right}</b>. "
                    "Resolve each one, then Continue.</span>")
        self.stop_info.setText(f"<span style='font-size:12pt'><b>Stopped at commit{step}</b>: {state.subject}"
                               f"</span><br>{text}")
        self.keep_left.setText(f"Keep {left} version")
        self.keep_right.setText(f"Keep {right} version")
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
        self.continue_btn.setEnabled(not state.conflicts and not state.empty)
        self.continue_btn.setToolTip("Resolve every file first" if state.conflicts else "")
        self.status.setText(f"{n} conflict(s) left" if n else "Skip it" if state.empty else "All resolved: Continue")

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
        self.run(lambda: (rebase.take_side(self.path, file, side), cherry_pick.progress(self.path))[1], "Resolving")

    def mark(self, file: str):
        if rebase.has_markers(self.path, file) and QMessageBox.question(
                self, "Conflict markers left", f"{file} still contains conflict markers. Mark it resolved anyway?") \
                != QMessageBox.Yes:
            return
        self.run(lambda: (rebase.mark_resolved(self.path, file), cherry_pick.progress(self.path))[1], "Resolving")

    def abort(self):
        box = QMessageBox(QMessageBox.Warning, "Abort cherry-pick",
                          "Stop, and put the destination branch back as it was before the cherry-pick?",
                          QMessageBox.Cancel, self)
        confirm = box.addButton("Abort cherry-pick", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is confirm:
            self.run(cherry_pick.resume, "Aborting", self.path, "abort", aborted=True)

    def refresh_state(self):
        self.run(cherry_pick.progress, "Checking", self.path)

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
        self.push_btn = QPushButton("Push")
        self.push_btn.clicked.connect(self.push)
        history = QPushButton("Open history")
        history.clicked.connect(lambda: self.main.open_tree(self.p))
        close = QPushButton("Close")
        close.setObjectName("primary")
        close.clicked.connect(self.close)
        for b in (self.push_btn, history, close):
            row.addWidget(b)
        row.addStretch()
        lay.addSpacing(16)
        lay.addLayout(row)
        lay.addStretch()
        return page

    def show_done(self, aborted: bool = False):
        state = cherry_pick.read_state(self.path)
        dest, back = state.get("dest", ""), state.get("back", "")
        go_back = getattr(self, "go_back", True)
        self.pushed_branch = dest
        self.pages.setCurrentIndex(2)
        self.done_title.setText("Cherry-pick aborted" if aborted else "✓  Cherry-picked")
        self.push_btn.setVisible(not aborted and bool(dest))
        self.push_btn.setText(f"Push {dest}")
        self.status.setText("Restoring…")

        def done(warning, error):
            self.status.setText("")
            self.main.refresh_project(self.p)
            where = f"<b>{dest}</b>"
            text = ("The destination branch is back as it was." if aborted else
                    f"{state.get('total', '')} commit(s) applied on {where}.")
            if go_back and back and back != dest:
                text += f"<br>You are back on <b>{back}</b>" + (", with your local changes." if state.get("stash")
                                                                else ".")
            elif not aborted:
                text += f"<br>You are on {where}."
            if error or warning:
                text += f"<br><br><span style='color:{C['orange']}'>{error or warning}</span>"
            self.done_text.setText(text)

        self.tasks.submit(cherry_pick.finish, done, self.path, go_back)

    def push(self):
        branch = getattr(self, "pushed_branch", "")
        if not branch:
            return
        cred = self.main.cred(self.p)
        self.status.setText(f"Pushing {branch}…")

        def work():
            run_git(["push", "-u", "origin", f"{branch}:{branch}"], cwd=self.path, cred=cred, timeout=300)

        def done(_r, err):
            self.status.setText("")
            self.main.refresh_project(self.p)
            if err:
                ErrorDialog(explain(str(err), "Push"), self.p.name, self).exec()
                return
            self.push_btn.hide()
            self.done_text.setText(self.done_text.text() + f"<br><br>Pushed <b>{branch}</b>.")

        self.tasks.submit_network(work, done)

    # ---------- plumbing ----------
    def run(self, fn, label: str, *args, aborted: bool = False):
        if self.busy:
            return
        self.busy = True
        self.start_btn.setEnabled(False)
        self.status.setText(f"{label}…")

        def done(state, err):
            self.busy = False
            self.status.setText("")
            self.main.refresh_project(self.p)
            if err:
                ErrorDialog(explain(str(err), label), self.p.name, self).exec()
                current = cherry_pick.progress(self.path)
                if current.active:
                    self.show_conflicts(current)
                else:
                    self.update_preview()
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
