import time

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import repo
from .errors import explain
from .repo import Branch
from .style import C
from .tree_window import rel_time
from .widgets import ElidedLabel, ErrorDialog, icon_button, keep_size


class NewBranchDialog(QDialog):
    def __init__(self, parent, starts: list[str], default_start: str):
        super().__init__(parent)
        self.setWindowTitle("New branch")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(8)
        lay.addWidget(QLabel("Name"))
        self.name = QLineEdit()
        self.name.setPlaceholderText("feature/my-change")
        lay.addWidget(self.name)
        lay.addWidget(QLabel("Start from"))
        self.start = QComboBox()
        self.start.setEditable(True)
        self.start.addItems(starts)
        self.start.setCurrentText(default_start)
        lay.addWidget(self.start)
        hint = QLabel("The new branch does not track its start point; Publish it to create it on origin.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.switch = QCheckBox("Switch to the new branch")
        self.switch.setChecked(True)
        lay.addWidget(self.switch)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Create")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(lambda: self.name.text().strip() and self.accept())
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)


class CleanupDialog(QDialog):
    """Pick local branches to delete: merged ones are checked, gone-but-unmerged ones are not."""

    def __init__(self, parent, merged: list[Branch], gone: list[Branch], base: str):
        super().__init__(parent)
        self.setWindowTitle("Clean up branches")
        self.setMinimumSize(520, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        info = QLabel(f"Branches merged into {base} are safe to delete. Branches whose remote branch is gone "
                      "but that are not merged may hold commits found nowhere else.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.list = QListWidget()
        self.list.setObjectName("files")
        for b, checked, note in [(b, True, f"merged into {base}") for b in merged] + \
                                [(b, False, "remote gone, NOT merged") for b in gone]:
            item = QListWidgetItem(f"{b.name}    ·    {note}")
            item.setData(Qt.UserRole, (b.name, not checked))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            if not checked:
                item.setForeground(QColor(C["orange"]))
            self.list.addItem(item)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Delete checked")
        ok.setObjectName("danger")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def checked(self) -> tuple[list[str], list[str]]:
        """(safe deletes, forced deletes)."""
        safe, forced = [], []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == Qt.Checked:
                name, force = item.data(Qt.UserRole)
                (forced if force else safe).append(name)
        return safe, forced


def _load(path: str, base: str):
    return repo.list_branch_info(path, base)


class BranchesWindow(QWidget):
    COLS = ["Branch", "State", "Tracking", "Last commit", "Date"]

    def __init__(self, main, project):
        super().__init__(main, Qt.Window)
        self.main, self.p, self.tasks = main, project, main.tasks
        self.path = project.path
        self.base = project.snap.base if project.snap else ""
        self.local: list[Branch] = []
        self.remote: list[Branch] = []
        self.busy = False
        self.last_refresh = 0.0

        self.setWindowTitle(f"Branches · {project.name}")
        self.resize(1200, 760)
        keep_size(self, self.main.config, "branches")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 14, 16, 14)
        title = QLabel(project.name)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        sub = QLabel("Branches")
        sub.setObjectName("branchChip")
        hl.addWidget(title)
        hl.addWidget(sub)
        self.status = ElidedLabel("", Qt.ElideRight)
        self.status.setObjectName("muted")
        self.status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hl.addWidget(self.status, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter branches  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self.populate)
        hl.addWidget(self.search)
        new = QPushButton("New branch…")
        new.setObjectName("primary")
        new.clicked.connect(self.new_branch)
        hl.addWidget(new)
        refresh = icon_button("refresh", "Refresh (F5)")
        refresh.clicked.connect(self.refresh)
        hl.addWidget(refresh)
        root.addWidget(header)

        bar = QWidget()
        bar.setObjectName("sidePanel")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 6)
        self.checkout_btn = QPushButton("Checkout")
        self.checkout_btn.clicked.connect(self.checkout)
        self.rename_btn = QPushButton("Rename…")
        self.rename_btn.clicked.connect(self.rename)
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.setObjectName("danger")
        self.delete_btn.clicked.connect(self.delete_local)
        self.delete_remote_btn = QPushButton("Delete on origin…")
        self.delete_remote_btn.setObjectName("danger")
        self.delete_remote_btn.clicked.connect(self.delete_remote)
        self.rebase_btn = QPushButton("Rebase onto…")
        self.rebase_btn.setToolTip("Move the commits of the selected branch onto another base")
        self.rebase_btn.clicked.connect(lambda: self.selected() and self.main.open_rebase(self.p, self.selected()[0].name))
        cleanup = QPushButton("Clean up merged…")
        cleanup.setToolTip("Delete local branches already merged into the base branch")
        cleanup.clicked.connect(self.cleanup)
        for b in (self.checkout_btn, self.rename_btn, self.delete_btn, self.delete_remote_btn):
            bl.addWidget(b)
        bl.addStretch()
        bl.addWidget(self.rebase_btn)
        bl.addWidget(cleanup)
        branches_page = QWidget()
        bp = QVBoxLayout(branches_page)
        bp.setContentsMargins(0, 0, 0, 0)
        bp.setSpacing(0)
        bp.addWidget(bar)

        self.tree = QTreeWidget()
        self.tree.setObjectName("branchTree")
        self.tree.setColumnCount(len(self.COLS))
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setAlternatingRowColors(False)
        hh = self.tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        for col, w in ((0, 330), (1, 150), (2, 200), (4, 110)):
            self.tree.setColumnWidth(col, w)
        self.tree.itemSelectionChanged.connect(self.update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self.checkout())
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        wrap = QWidget()
        wrap.setObjectName("sidePanel")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(16, 4, 16, 16)
        wl.addWidget(self.tree)
        bp.addWidget(wrap, 1)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(branches_page, "Branches")
        self.tags = TagsPage(self)
        self.tabs.addTab(self.tags, "Tags")
        self.tabs.currentChanged.connect(lambda i: i == 1 and self.tags.refresh())
        root.addWidget(self.tabs, 1)

        QShortcut(QKeySequence("F5"), self, self.refresh)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.search.setFocus())
        QShortcut(QKeySequence("Delete"), self.tree, self.delete_local)
        self.update_buttons()
        self.refresh()

    # ---------- data ----------
    def refresh(self):
        self.last_refresh = time.monotonic()
        self.status.setText("Loading…")
        self.tasks.submit(_load, self._on_load, self.path, self.base)
        if getattr(self, "tabs", None) is not None and self.tabs.currentIndex() == 1:
            self.tags.refresh()

    def _on_load(self, result, error):
        self.status.setText("")
        if error:
            self.status.setText(str(error))
            return
        self.local, self.remote = result
        self.populate()

    def populate(self):
        term = self.search.text().strip().lower()
        selected = {b.name for b in self.selected()}
        scroll = self.tree.verticalScrollBar().value()
        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for title, branches in (("Local", self.local), ("Remote · origin", self.remote)):
            shown = [b for b in branches if not term or term in b.name.lower()]
            group = QTreeWidgetItem([f"{title}   {len(shown)}"])
            group.setFlags(Qt.ItemIsEnabled)
            group.setFont(0, bold)
            group.setForeground(0, QColor(C["muted"]))
            self.tree.addTopLevelItem(group)
            for b in shown:
                item = QTreeWidgetItem(self._columns(b))
                item.setData(0, Qt.UserRole, b)
                item.setToolTip(3, b.subject)
                item.setToolTip(4, time.strftime("%Y-%m-%d %H:%M", time.localtime(b.time)))
                if b.current:
                    item.setFont(0, bold)
                    item.setForeground(0, QColor(C["green"]))
                for col, color in ((1, self._state_color(b)), (2, self._tracking_color(b)), (3, C["muted"]),
                                   (4, C["faint"])):
                    item.setForeground(col, QColor(color))
                group.addChild(item)
                if b.name in selected:
                    item.setSelected(True)
            group.setExpanded(True)
        self.tree.verticalScrollBar().setValue(scroll)
        self.update_buttons()

    def _columns(self, b: Branch) -> list[str]:
        states = []
        if b.current:
            states.append("current")
        if not b.remote and b.name == self.base:
            states.append("base")
        if b.merged:
            states.append("merged")
        if b.gone:
            states.append("remote gone")
        if b.remote and b.has_local:
            states.append("checked out")
        if b.remote:
            tracking = ""
        elif b.gone:
            tracking = f"{b.upstream} (gone)"
        elif b.upstream:
            ab = " ".join(x for x in (f"↑{b.ahead}" if b.ahead else "", f"↓{b.behind}" if b.behind else "") if x)
            tracking = f"{b.upstream}  {ab or '✓'}"
        else:
            tracking = "not published"
        return [("●  " if b.current else "") + b.name, ", ".join(states), tracking, b.subject, rel_time(b.time)]

    @staticmethod
    def _state_color(b: Branch) -> str:
        return C["orange"] if b.gone else C["green"] if b.merged else C["muted"]

    @staticmethod
    def _tracking_color(b: Branch) -> str:
        if b.gone:
            return C["orange"]
        if b.behind:
            return C["orange"]
        if b.ahead:
            return C["blue"]
        return C["faint"] if not b.upstream else C["muted"]

    def selected(self) -> list[Branch]:
        return [i.data(0, Qt.UserRole) for i in self.tree.selectedItems() if i.data(0, Qt.UserRole)]

    def update_buttons(self):
        sel = self.selected()
        one = sel[0] if len(sel) == 1 else None
        free = not self.busy and not (self.p.snap and self.p.snap.op)
        self.checkout_btn.setEnabled(bool(one) and not one.current and free)
        self.rebase_btn.setEnabled(bool(one) and not one.remote and free)
        self.rename_btn.setEnabled(bool(one) and not one.remote and free)
        self.delete_btn.setEnabled(bool(sel) and all(not b.remote and not b.current for b in sel) and free)
        self.delete_remote_btn.setEnabled(bool(one) and one.remote and free)

    # ---------- actions ----------
    def _run(self, fn, label: str, *args, then=None, network: bool = False):
        self.busy = True
        self.status.setText(f"{label}…")
        self.update_buttons()

        def done(result, err):
            self.busy = False
            self.status.setText("" if err else f"{label} done")
            if err:
                if then and then(err):
                    return
                ErrorDialog(explain(str(err), label), self.p.name, self).exec()
            self.refresh()
            self.main.refresh_project(self.p)

        (self.tasks.submit_network if network else self.tasks.submit)(fn, done, *args)

    def checkout(self):
        sel = self.selected()
        if len(sel) != 1 or sel[0].current or self.busy:
            return
        b = sel[0]
        # A remote branch is checked out through its local name; git creates the tracking branch.
        self.main.switch_branch(self.p, b.name.split("/", 1)[1] if b.remote else b.name)

    def new_branch(self):
        current = next((b.name for b in self.local if b.current), "")
        starts = [s for s in dict.fromkeys([current, self.base, f"origin/{self.base}" if self.base else ""]) if s]
        sel = self.selected()
        if len(sel) == 1 and sel[0].name not in starts:
            starts.append(sel[0].name)
        if len(sel) == 1:
            default = sel[0].name
        elif any(r.name == f"origin/{self.base}" for r in self.remote):
            default = f"origin/{self.base}"  # Fresh start from the remote base, even if local is behind.
        else:
            default = current
        dialog = NewBranchDialog(self, starts, default)
        if not dialog.exec():
            return
        name, start, switch = dialog.name.text().strip(), dialog.start.currentText().strip(), dialog.switch.isChecked()
        self.busy = True
        self.status.setText("Creating…")

        def done(_r, err):
            self.busy = False
            self.status.setText("")
            if err:
                ErrorDialog(explain(str(err), "Create branch"), self.p.name, self).exec()
                return
            self.status.setText(f"Created {name}")
            self.refresh()
            if switch:
                self.main.switch_branch(self.p, name)

        self.tasks.submit(repo.create_branch, done, self.path, name, start)

    def rename(self):
        sel = self.selected()
        if len(sel) != 1 or sel[0].remote:
            return
        b = sel[0]
        note = ("\n\nThe branch on origin keeps its old name; publish the new name, "
                "then delete the old one on origin if needed.") if b.upstream and not b.gone else ""
        name, ok = QInputDialog.getText(self, "Rename branch", f"New name for {b.name}:{note}", text=b.name)
        name = name.strip()
        if ok and name and name != b.name:
            self._run(repo.rename_branch, "Rename", self.path, b.name, name)

    def delete_local(self):
        sel = [b for b in self.selected() if not b.remote and not b.current]
        if not sel or self.busy:
            return
        names = [b.name for b in sel]
        unmerged = [b.name for b in sel if not b.merged]
        text = f"Delete {len(names)} local branch{'es' if len(names) > 1 else ''}?\n\n" + "\n".join(names[:15])
        if unmerged:
            text += (f"\n\nNot merged into {self.base or 'the base branch'}: {', '.join(unmerged[:5])}. "
                     "Git refuses to delete those unless you force it.")
        box = QMessageBox(QMessageBox.Warning, "Delete branches", text, QMessageBox.Cancel, self)
        confirm = box.addButton("Delete", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not confirm:
            return

        def not_merged(err) -> bool:
            if "not fully merged" not in str(err):
                return False
            force = QMessageBox(QMessageBox.Warning, "Force delete",
                                "Some branches are not fully merged: their unmerged commits will be lost "
                                "unless they exist elsewhere.\n\nDelete them anyway?", QMessageBox.Cancel, self)
            yes = force.addButton("Force delete", QMessageBox.DestructiveRole)
            force.setDefaultButton(QMessageBox.Cancel)
            force.exec()
            if force.clickedButton() is yes:
                self._run(repo.delete_branches, "Force delete", self.path, names, True)
            else:
                self.refresh()
            return True

        self._run(repo.delete_branches, "Delete", self.path, names, False, then=not_merged)

    def delete_remote(self):
        sel = self.selected()
        if len(sel) != 1 or not sel[0].remote or self.busy:
            return
        if self.p.mismatch:
            QMessageBox.warning(self, "Delete on origin", "Origin URL differs from the configured URL: fix the remote first.")
            return
        name = sel[0].name.split("/", 1)[1]
        box = QMessageBox(QMessageBox.Warning, "Delete on origin",
                          f"Delete the branch \"{name}\" on the server (origin)?\n\n"
                          "This affects everyone using the repository and cannot be undone from here.",
                          QMessageBox.Cancel, self)
        confirm = box.addButton("Delete on origin", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is confirm:
            self._run(repo.delete_remote_branch, "Delete on origin", self.path, name, self.main.cred(self.p),
                      network=True)

    def cleanup(self):
        merged = [b for b in self.local if b.merged and not b.current and b.name != self.base]
        gone = [b for b in self.local if b.gone and not b.merged and not b.current and b.name != self.base]
        if not merged and not gone:
            QMessageBox.information(self, "Clean up branches", "No merged or orphaned local branch to delete.")
            return
        dialog = CleanupDialog(self, merged, gone, self.base or "the base branch")
        if not dialog.exec():
            return
        safe, forced = dialog.checked()

        def run_all():
            if safe:
                repo.delete_branches(self.path, safe, False)
            if forced:
                repo.delete_branches(self.path, forced, True)

        if safe or forced:
            self._run(run_all, f"Deleting {len(safe) + len(forced)} branch(es)")

    def context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item or not item.data(0, Qt.UserRole):
            return
        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
        menu = QMenu(self)
        for btn in (self.checkout_btn, self.rename_btn, self.delete_btn, self.delete_remote_btn):
            act = QAction(btn.text(), menu)
            act.setEnabled(btn.isEnabled())
            act.triggered.connect(btn.click)
            menu.addAction(act)
        menu.addSeparator()
        new = QAction("New branch from here…", menu)
        new.triggered.connect(self.new_branch)
        menu.addAction(new)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def changeEvent(self, event):
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow() and not self.busy
                and time.monotonic() - self.last_refresh > 3):
            self.refresh()
        super().changeEvent(event)


class NewTagDialog(QDialog):
    def __init__(self, parent, targets: list[str]):
        super().__init__(parent)
        self.setWindowTitle("New tag")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(8)
        lay.addWidget(QLabel("Name"))
        self.name = QLineEdit()
        self.name.setPlaceholderText("v1.2.0")
        lay.addWidget(self.name)
        lay.addWidget(QLabel("On commit"))
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.addItems(targets)
        lay.addWidget(self.target)
        lay.addWidget(QLabel("Message (optional: makes an annotated tag)"))
        self.message = QPlainTextEdit()
        self.message.setFixedHeight(70)
        lay.addWidget(self.message)
        self.push = QCheckBox("Push the tag to origin")
        lay.addWidget(self.push)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Create")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(lambda: self.name.text().strip() and self.accept())
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)


class TagsPage(QWidget):
    COLS = ["Tag", "Commit", "Subject", "Date", "On origin"]

    def __init__(self, win: "BranchesWindow"):
        super().__init__()
        self.win = win
        self.tags: list[repo.Tag] = []
        self.remote: set[str] | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("sidePanel")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 6)
        new = QPushButton("New tag…")
        new.setObjectName("primary")
        new.clicked.connect(self.new_tag)
        self.push_btn = QPushButton("Push")
        self.push_btn.setToolTip("Push the selected tags to origin")
        self.push_btn.clicked.connect(self.push)
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.setObjectName("danger")
        self.delete_btn.clicked.connect(self.delete_local)
        self.delete_remote_btn = QPushButton("Delete on origin…")
        self.delete_remote_btn.setObjectName("danger")
        self.delete_remote_btn.clicked.connect(self.delete_remote)
        check = QPushButton("Check origin")
        check.setToolTip("Ask the server which tags it has (network)")
        check.clicked.connect(self.check_remote)
        for b in (new, self.push_btn, self.delete_btn, self.delete_remote_btn):
            bl.addWidget(b)
        bl.addStretch()
        bl.addWidget(check)
        lay.addWidget(bar)
        self.tree = QTreeWidget()
        self.tree.setObjectName("branchTree")
        self.tree.setColumnCount(len(self.COLS))
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        hh = self.tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        for col, w in ((0, 220), (1, 90), (3, 110), (4, 90)):
            self.tree.setColumnWidth(col, w)
        self.tree.itemSelectionChanged.connect(self.update_buttons)
        wrap = QWidget()
        wrap.setObjectName("sidePanel")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(16, 4, 16, 16)
        wl.addWidget(self.tree)
        lay.addWidget(wrap, 1)
        win.search.textChanged.connect(self.populate)
        self.update_buttons()

    def refresh(self):
        self.win.tasks.submit(repo.list_tags, self._on_tags, self.win.path)

    def _on_tags(self, tags, error):
        if error:
            self.win.status.setText(str(error))
            return
        self.tags = tags
        self.populate()

    def populate(self):
        term = self.win.search.text().strip().lower()
        selected = {t.name for t in self.selected()}
        self.tree.clear()
        for t in self.tags:
            if term and term not in t.name.lower():
                continue
            origin = "" if self.remote is None else ("✓" if t.name in self.remote else "not pushed")
            item = QTreeWidgetItem([t.name, t.sha[:8], t.message or t.subject, rel_time(t.time), origin])
            item.setData(0, Qt.UserRole, t)
            item.setToolTip(2, (t.message + "\n\n" if t.message else "") + t.subject)
            item.setForeground(1, QColor(C["faint"]))
            item.setForeground(3, QColor(C["faint"]))
            item.setForeground(4, QColor(C["green"] if origin == "✓" else C["orange"]))
            if t.annotated:
                item.setToolTip(0, "Annotated tag")
            self.tree.addTopLevelItem(item)
            item.setSelected(t.name in selected)
        self.update_buttons()

    def selected(self) -> list:
        return [i.data(0, Qt.UserRole) for i in self.tree.selectedItems()]

    def update_buttons(self):
        sel = self.selected()
        self.push_btn.setEnabled(bool(sel))
        self.delete_btn.setEnabled(bool(sel))
        self.delete_remote_btn.setEnabled(len(sel) == 1)

    def _run(self, fn, label, *args, network=False):
        win = self.win
        win.status.setText(f"{label}…")

        def done(_r, err):
            win.status.setText("" if err else f"{label} done")
            if err:
                ErrorDialog(explain(str(err), label), win.p.name, win).exec()
            self.refresh()
            if network and not err:
                self.check_remote()

        (win.tasks.submit_network if network else win.tasks.submit)(fn, done, *args)

    def new_tag(self):
        current = next((b.name for b in self.win.local if b.current), "HEAD")
        dialog = NewTagDialog(self, list(dict.fromkeys(["HEAD", current, self.win.base] if self.win.base
                                                       else ["HEAD", current])))
        if not dialog.exec():
            return
        name, target = dialog.name.text().strip(), dialog.target.currentText().strip() or "HEAD"
        message, push = dialog.message.toPlainText().strip(), dialog.push.isChecked()
        cred = self.win.main.cred(self.win.p)

        def work():
            repo.create_tag(self.win.path, name, target, message)
            if push:
                repo.push_tags(self.win.path, [name], cred)

        self._run(work, f"Create tag {name}", network=push)

    def push(self):
        names = [t.name for t in self.selected()]
        if names and not self._mismatch():
            self._run(repo.push_tags, f"Push {len(names)} tag(s)", self.win.path, names,
                      self.win.main.cred(self.win.p), network=True)

    def delete_local(self):
        names = [t.name for t in self.selected()]
        if not names:
            return
        box = QMessageBox(QMessageBox.Warning, "Delete tags",
                          f"Delete {len(names)} local tag(s)?\n\n" + "\n".join(names[:15])
                          + "\n\nTags already pushed stay on origin.", QMessageBox.Cancel, self)
        confirm = box.addButton("Delete", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is confirm:
            self._run(lambda: [repo.delete_tag(self.win.path, n) for n in names], "Delete tags")

    def delete_remote(self):
        sel = self.selected()
        if len(sel) != 1 or self._mismatch():
            return
        name = sel[0].name
        box = QMessageBox(QMessageBox.Warning, "Delete tag on origin",
                          f"Delete the tag \"{name}\" on the server (origin)?\n\nThis affects everyone using the "
                          "repository.", QMessageBox.Cancel, self)
        confirm = box.addButton("Delete on origin", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is confirm:
            self._run(repo.delete_remote_tag, f"Delete {name} on origin", self.win.path, name,
                      self.win.main.cred(self.win.p), network=True)

    def check_remote(self):
        if self._mismatch():
            return
        self.win.status.setText("Asking origin for its tags…")

        def done(names, err):
            self.win.status.setText("" if err else f"origin has {len(names)} tag(s)")
            if err:
                ErrorDialog(explain(str(err), "List remote tags"), self.win.p.name, self.win).exec()
                return
            self.remote = names
            self.populate()

        self.win.tasks.submit_network(repo.remote_tags, done, self.win.path, self.win.main.cred(self.win.p))

    def _mismatch(self) -> bool:
        if self.win.p.mismatch:
            QMessageBox.warning(self, "Tags", "Origin URL differs from the configured URL: fix the remote first.")
            return True
        return False
