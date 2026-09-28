import time

from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSplitter, QStyle, QStyledItemDelegate, QVBoxLayout,
                               QWidget)

from . import repo
from .changes_window import tree_toggle
from .diff_view import DiffView, parse_diff
from .errors import explain
from .repo import FileChange, Stash
from .style import C
from .tree_window import rel_time
from .file_view import ExtensionBar, FileView, extension
from .widgets import ElidedLabel, ErrorDialog, icon_button


class StashDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 50)

    def paint(self, p: QPainter, option, index):
        st: Stash = index.data(Qt.UserRole)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(4, 2, -4, -2)
        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#262c3a"))
            p.drawRoundedRect(r, 7, 7)
        elif option.state & QStyle.State_MouseOver:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["hover"]))
            p.drawRoundedRect(r, 7, 7)
        # "On branch: message" / "WIP on branch: sha subject" -> branch and message.
        branch, _, msg = st.message.partition(": ")
        branch = branch.removeprefix("On ").removeprefix("WIP on ")
        font = QFont(option.font)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        p.setPen(QColor(C["text"]))
        top = QRectF(r.left() + 12, r.top() + 5, r.width() - 24, 20)
        p.drawText(top, Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(msg or st.message, Qt.ElideRight, int(top.width())))
        small = QFont(option.font)
        small.setPointSizeF(8.5)
        p.setFont(small)
        p.setPen(QColor(C["faint"]))
        bottom = QRectF(r.left() + 12, r.top() + 25, r.width() - 24, 18)
        p.drawText(bottom, Qt.AlignLeft | Qt.AlignVCenter, f"{st.ref}   ·   {branch}   ·   {rel_time(st.time)}")
        p.restore()


class StashNowDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stash changes")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.addWidget(QLabel("Message (optional)"))
        self.message = QLineEdit()
        self.message.setPlaceholderText("e.g. WIP login form")
        lay.addWidget(self.message)
        self.untracked = QCheckBox("Include untracked files")
        self.untracked.setChecked(True)
        lay.addWidget(self.untracked)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Stash")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)


def _load(path: str):
    return repo.stashes(path)


class StashWindow(QWidget):
    def __init__(self, main, project):
        super().__init__(main, Qt.Window)
        self.main, self.p, self.tasks = main, project, main.tasks
        self.path = project.path
        self.items: list[Stash] = []
        self.stash: Stash | None = None
        self.file: FileChange | None = None
        self.busy = False
        self.last_refresh = 0.0

        self.setWindowTitle(f"Stashes · {project.name}")
        self.resize(1360, 780)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 14, 16, 14)
        title = QLabel(project.name)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        sub = QLabel("Stashes")
        sub.setObjectName("branchChip")
        hl.addWidget(title)
        hl.addWidget(sub)
        self.status = ElidedLabel("", Qt.ElideRight)
        self.status.setObjectName("muted")
        self.status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hl.addWidget(self.status, 1)
        stash_now = QPushButton("Stash current changes…")
        stash_now.clicked.connect(self.stash_now)
        hl.addWidget(stash_now)
        refresh = icon_button("refresh", "Refresh (F5)")
        refresh.clicked.connect(self.refresh)
        hl.addWidget(refresh)
        root.addWidget(header)

        left = QWidget()
        left.setObjectName("sidePanel")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(12, 12, 12, 12)
        ll.setSpacing(8)
        self.count = QLabel("Stashes")
        self.count.setObjectName("sectionTitle")
        self.count.setTextFormat(Qt.RichText)
        ll.addWidget(self.count)
        self.list = QListWidget()
        self.list.setObjectName("files")
        self.list.setItemDelegate(StashDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.currentItemChanged.connect(self.on_stash)
        ll.addWidget(self.list, 3)
        buttons = QHBoxLayout()
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setToolTip("Reapply the changes and keep the stash")
        self.apply_btn.clicked.connect(lambda: self.apply(False))
        self.pop_btn = QPushButton("Pop")
        self.pop_btn.setObjectName("primary")
        self.pop_btn.setToolTip("Reapply the changes, then delete the stash if it applied cleanly")
        self.pop_btn.clicked.connect(lambda: self.apply(True))
        self.drop_btn = QPushButton("Drop…")
        self.drop_btn.setObjectName("danger")
        self.drop_btn.setToolTip("Delete this stash")
        self.drop_btn.clicked.connect(self.drop)
        for b in (self.pop_btn, self.apply_btn, self.drop_btn):
            buttons.addWidget(b)
        ll.addLayout(buttons)
        self.files_title = QLabel("Files")
        self.files_title.setObjectName("sectionTitle")
        files_head = QHBoxLayout()
        files_head.addWidget(self.files_title)
        files_head.addStretch()
        self.files = FileView(main.config.file_tree)
        self.files.selection_changed.connect(self.on_file)
        files_head.addWidget(tree_toggle(main, [self.files]))
        ll.addLayout(files_head)
        self.ext_bar = ExtensionBar()
        self.ext_bar.toggled.connect(self.set_ext)
        self.ext = ""
        self.all_files: list[FileChange] = []
        ll.addWidget(self.ext_bar)
        ll.addWidget(self.files, 2)

        self.diff = DiffView()
        self.diff.set_message("Select a stash")
        self.diff.options_changed.connect(lambda: self.file and self.show_diff(self.file))
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(1)
        split.addWidget(left)
        split.addWidget(self.diff)
        split.setSizes([420, 940])
        root.addWidget(split, 1)
        QShortcut(QKeySequence("F5"), self, self.refresh)
        self.update_buttons()
        self.refresh()

    def refresh(self):
        self.last_refresh = time.monotonic()
        self.tasks.submit(_load, self._on_load, self.path)

    def _on_load(self, items, error):
        if error:
            self.status.setText(str(error))
            return
        keep = self.stash.sha if self.stash else None
        same = [s.sha for s in items] == [s.sha for s in self.items]
        self.items = items
        n = len(items)
        self.count.setText(f"Stashes <span style='color:{C['faint']}'>&nbsp;{n}</span>")
        if same and self.list.count() == n:
            # Refs may have been renumbered by git; update data, keep the view as it is.
            for i, st in enumerate(items):
                self.list.item(i).setData(Qt.UserRole, st)
                if st.sha == keep:
                    self.stash = st
            return
        self.list.blockSignals(True)
        self.list.clear()
        for st in items:
            item = QListWidgetItem(st.message)
            item.setData(Qt.UserRole, st)
            item.setToolTip(time.strftime("%Y-%m-%d %H:%M", time.localtime(st.time)))
            self.list.addItem(item)
        self.list.blockSignals(False)
        row = next((i for i, s in enumerate(items) if s.sha == keep), 0 if items else -1)
        if row >= 0:
            self.list.setCurrentRow(row)
        else:
            self.stash = None
            self.files.set_files([])
            self.diff.set_message("No stash in this repository")
        self.update_buttons()

    def on_stash(self, item, _prev=None):
        if item is None:
            return
        st = item.data(Qt.UserRole)
        if self.stash and st.sha == self.stash.sha:
            self.stash = st
            return
        self.stash, self.file = st, None
        self.files.set_files([])
        self.diff.set_message("Select a file to see its diff")
        self.tasks.submit(repo.stash_files, lambda files, err: self._on_files(st, files, err), self.path, st)
        self.update_buttons()

    def _on_files(self, st: Stash, files, error):
        if not self.stash or self.stash.sha != st.sha:
            return
        if error:
            self.diff.set_message(str(error))
            return
        self.files_title.setText(f"{len(files)} file{'s' if len(files) != 1 else ''}")
        self.all_files, self.ext = files, ""
        self.show_files()

    def show_files(self):
        shown = [f for f in self.all_files if not self.ext or extension(f.path) == self.ext]
        self.files.set_files(shown, ("", ""))
        self.ext_bar.set_files(self.all_files, self.ext)
        self.files.select_first()

    def set_ext(self, ext: str):
        self.ext = ext
        self.show_files()

    def on_file(self):
        fc = self.files.current_file()
        if fc and fc is not self.file:
            self.file = fc
            self.show_diff(fc)

    def show_diff(self, fc: FileChange):
        st, ws, full = self.stash, self.diff.ignore_ws, self.diff.full_file
        self.diff.set_message("Loading…", fc.path)
        self.tasks.submit(lambda: parse_diff(repo.stash_diff(self.path, st, fc, ws, full)),
                          lambda data, err: self._on_diff(st, fc, data, err))

    def _on_diff(self, st, fc, data, error):
        if self.stash is not st or self.file is not fc:
            return
        if error:
            self.diff.set_message(str(error), fc.path)
        else:
            self.diff.set_diff(fc.path, data)

    def update_buttons(self):
        ok = self.stash is not None and not self.busy
        for b in (self.apply_btn, self.pop_btn, self.drop_btn):
            b.setEnabled(ok)

    def _run(self, fn, label: str, *args):
        self.busy = True
        self.status.setText(f"{label}…")
        self.update_buttons()

        def done(_r, err):
            self.busy = False
            self.status.setText("" if err else f"{label} done")
            if err:
                ErrorDialog(explain(str(err), label), self.p.name, self).exec()
            self.refresh()
            self.main.refresh_project(self.p)
            self.main.refresh_windows(self.p, exclude=self)

        self.tasks.submit(fn, done, *args)

    def apply(self, pop: bool):
        if self.stash and not self.busy:
            self._run(repo.stash_apply, "Pop" if pop else "Apply", self.path, self.stash, pop)

    def drop(self):
        if not self.stash or self.busy:
            return
        box = QMessageBox(QMessageBox.Warning, "Drop stash",
                          f"Delete {self.stash.ref}?\n\n{self.stash.message}\n\nThis cannot be undone.",
                          QMessageBox.Cancel, self)
        confirm = box.addButton("Drop", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is confirm:
            self._run(repo.stash_drop, "Drop", self.path, self.stash)

    def stash_now(self):
        if self.busy:
            return
        dialog = StashNowDialog(self)
        if dialog.exec():
            self._run(repo.stash_push, "Stash", self.path, dialog.message.text().strip(),
                      dialog.untracked.isChecked())

    def changeEvent(self, event):
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow() and not self.busy
                and time.monotonic() - self.last_refresh > 3):
            self.refresh()
        super().changeEvent(event)
