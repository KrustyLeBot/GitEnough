"""Merge-request view: what the current branch changes compared with a target branch."""

import time

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QSplitter,
                               QVBoxLayout, QWidget)

from . import repo
from .changes_window import tree_toggle
from .diff_view import DiffView, parse_diff
from .file_view import ExtensionBar, FileView, extension
from .repo import Comparison, FileChange
from .style import C
from .tree_window import rel_time
from .widgets import ElidedLabel, icon_button


def _targets(project) -> list[str]:
    snap = project.snap
    base = snap.base if snap else ""
    names = list(snap.branches) if snap else []
    first = [f"origin/{base}"] if base and base in snap.on_origin else []
    if base:
        first.append(base)
    current = snap.status.branch if snap and snap.status else None
    return list(dict.fromkeys(first + [f"origin/{n}" for n in names if n in snap.on_origin and n != current]
                              + [n for n in names if n != current]))


class CompareWindow(QWidget):
    def __init__(self, main, project):
        super().__init__(main, Qt.Window)
        self.main, self.p, self.tasks = main, project, main.tasks
        self.path = project.path
        self.cmp: Comparison | None = None
        self.file: FileChange | None = None
        self.ext = ""
        self.last_refresh = 0.0

        branch = project.status.branch if project.status else "HEAD"
        self.setWindowTitle(f"Compare · {project.name}")
        self.resize(1440, 860)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 14, 16, 14)
        title = QLabel(project.name)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        chip = QLabel(f"⎇  {branch}")
        chip.setObjectName("branchChip")
        arrow = QLabel("compared with")
        arrow.setObjectName("muted")
        self.target = QComboBox()
        self.target.setMinimumWidth(220)
        self.target.addItems(_targets(project))
        self.target.setEditable(True)
        self.target.activated.connect(lambda *_: self.refresh())
        self.worktree = QCheckBox("Include uncommitted changes")
        self.worktree.toggled.connect(self.refresh)
        for w in (title, chip, arrow, self.target, self.worktree):
            hl.addWidget(w)
        self.summary = ElidedLabel("", Qt.ElideRight)
        self.summary.setObjectName("muted")
        self.summary.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hl.addWidget(self.summary, 1)
        refresh = icon_button("refresh", "Refresh (F5)")
        refresh.clicked.connect(self.refresh)
        hl.addWidget(refresh)
        root.addWidget(header)

        left = QWidget()
        left.setObjectName("sidePanel")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(12, 12, 12, 12)
        ll.setSpacing(6)
        self.commits_title = QLabel("Commits")
        self.commits_title.setObjectName("sectionTitle")
        ll.addWidget(self.commits_title)
        self.commits = QListWidget()
        self.commits.setObjectName("files")
        self.commits.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ll.addWidget(self.commits, 1)
        head = QHBoxLayout()
        self.files_title = QLabel("Files")
        self.files_title.setObjectName("sectionTitle")
        self.files_title.setTextFormat(Qt.RichText)
        head.addWidget(self.files_title)
        head.addStretch()
        self.files = FileView(main.config.file_tree)
        self.files.selection_changed.connect(self.on_file)
        head.addWidget(tree_toggle(main, [self.files]))
        ll.addLayout(head)
        self.ext_bar = ExtensionBar()
        self.ext_bar.toggled.connect(self.set_ext)
        ll.addWidget(self.ext_bar)
        ll.addWidget(self.files, 3)

        self.diff = DiffView()
        self.diff.set_message("Select a file")
        self.diff.options_changed.connect(lambda: self.file and self.show_diff(self.file))
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(1)
        split.addWidget(left)
        split.addWidget(self.diff)
        split.setSizes([440, 1000])
        root.addWidget(split, 1)
        QShortcut(QKeySequence("F5"), self, self.refresh)
        self.refresh()

    def refresh(self):
        target = self.target.currentText().strip()
        if not target:
            self.summary.setText("No branch to compare with")
            return
        self.last_refresh = time.monotonic()
        self.summary.setText("Comparing…")
        self.tasks.submit(repo.compare, self._on_compare, self.path, target, self.worktree.isChecked())

    def _on_compare(self, cmp: Comparison, error):
        if error:
            self.summary.setText(f"Cannot compare: {error}")
            return
        self.cmp = cmp
        n, m = len(cmp.commits), len(cmp.files)
        self.summary.setText(f"{n} commit{'s' if n != 1 else ''} ahead of {cmp.target}  ·  {m} file{'s' if m != 1 else ''}"
                             f"  ·  merge base {cmp.merge_base[:8]}")
        self.commits_title.setText(f"{n} commit{'s' if n != 1 else ''} on this branch")
        self.commits.clear()
        for c in cmp.commits:
            item = QListWidgetItem(f"{c.subject}\n{c.sha[:8]}  ·  {c.author}  ·  {rel_time(c.time)}")
            item.setToolTip(c.subject)
            self.commits.addItem(item)
        self.populate()

    def set_ext(self, ext: str):
        self.ext = ext
        self.populate()

    def populate(self):
        if not self.cmp:
            return
        files = [f for f in self.cmp.files if not self.ext or extension(f.path) == self.ext]
        keep = self.file.key if self.file else None
        self.files.set_files(files, keep)
        self.ext_bar.set_files(self.cmp.files, self.ext)
        total = len(self.cmp.files)
        count = f"{len(files)} / {total}" if len(files) != total else str(total)
        self.files_title.setText(f"Files <span style='color:{C['faint']}'>&nbsp;{count}</span>")
        if not self.files.current_file() and not self.files.select_first():
            self.file = None
            self.diff.set_message("No difference" if not total else "No file matches the filter")

    def on_file(self):
        fc = self.files.current_file()
        if fc and (self.file is None or fc.key != self.file.key or self.diff.data is None):
            self.file = fc
            self.show_diff(fc)

    def show_diff(self, fc: FileChange):
        cmp, ws, full = self.cmp, self.diff.ignore_ws, self.diff.full_file
        if not (self.diff.data is not None and self.diff.filename == fc.path):
            self.diff.set_message("Loading…", fc.path)
        self.tasks.submit(lambda: parse_diff(repo.compare_diff(self.path, cmp, fc, ws, full)),
                          lambda data, err: self._on_diff(cmp, fc, data, err))

    def _on_diff(self, cmp, fc, data, error):
        if self.cmp is not cmp or self.file is not fc:
            return
        if error:
            self.diff.set_message(str(error), fc.path)
        else:
            self.diff.set_diff(fc.path, data, keep_scroll=True)

    def changeEvent(self, event):
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow()
                and time.monotonic() - self.last_refresh > 5 and self.worktree.isChecked()):
            self.refresh()
        super().changeEvent(event)
