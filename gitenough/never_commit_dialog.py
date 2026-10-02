from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from . import never_commit
from .diff_view import mono_font
from .errors import explain
from .style import C
from .widgets import ErrorDialog


def _changed(e: dict) -> tuple[list[str], list[str], int]:
    """The lines of an entry without its context: (in the commits, kept on disk, first line number)."""
    pre, post = never_commit._common_context(e)
    return e["a"][pre:len(e["a"]) - post], e["b"][pre:len(e["b"]) - post], e.get("line", 0) + pre + 1


class NeverCommitDialog(QDialog):
    """Lists the never-commit lines of a repository: allow committing them again, or put back lost ones."""

    def __init__(self, parent, path: str, name: str):
        super().__init__(parent)
        self.path, self.name = path, name
        self.setWindowTitle(f"Never-commit lines · {name}")
        self.resize(860, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(8)
        head = QLabel("Never-commit lines")
        head.setStyleSheet("font-size: 12pt; font-weight: 700;")
        lay.addWidget(head)
        note = QLabel("These lines stay in your files but are left out of the changes, the stashes and every "
                      "commit. Lines marked \"no longer matches\" were changed by hand or by git (pull, switch): "
                      "they show as regular changes until you put them back or forget them.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Line", "Kept lines", "State"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._on_selection)
        lay.addWidget(self.tree, 2)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono_font())
        self.preview.setPlaceholderText("Select an entry to see its lines")
        lay.addWidget(self.preview, 1)

        row = QHBoxLayout()
        self.put_back = QPushButton("Put back in the file")
        self.put_back.setToolTip("Write the kept lines into the file again, where their surrounding lines still fit")
        self.put_back.clicked.connect(self._put_back)
        self.allow = QPushButton("Allow committing")
        self.allow.setToolTip("Forget the selected entries: their lines stay in the file and show up as "
                              "regular changes again")
        self.allow.clicked.connect(self._allow)
        close = QPushButton("Close")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        row.addWidget(self.put_back)
        row.addWidget(self.allow)
        row.addStretch()
        row.addWidget(close)
        lay.addLayout(row)
        self.load()

    def load(self):
        self.tree.clear()
        self.items: list[tuple[dict, bool]] = never_commit.status(self.path)
        # Whole new files, left out of git through info/exclude: always "kept".
        self.items += [({"file": f, "whole": True}, True) for f in never_commit.kept_files(self.path)]
        for i, (e, found) in enumerate(self.items):
            if e.get("whole"):
                item = QTreeWidgetItem([e["file"], "", "whole new file", "kept"])
                item.setData(0, Qt.UserRole, i)
                self.tree.addTopLevelItem(item)
                continue
            _old, new, number = _changed(e)
            first = next((t.strip() for t in new if t.strip()), "(lines removed)")
            item = QTreeWidgetItem([e["file"], str(number), first,
                                    "kept" if found else "no longer matches"])
            item.setData(0, Qt.UserRole, i)
            if not found:
                item.setForeground(3, QColor(C["orange"]))
            self.tree.addTopLevelItem(item)
        for col in (0, 1, 3):
            self.tree.resizeColumnToContents(col)
        self._on_selection()

    def _chosen(self) -> list[tuple[dict, bool]]:
        return [self.items[it.data(0, Qt.UserRole)] for it in self.tree.selectedItems()]

    def _on_selection(self):
        chosen = self._chosen()
        self.allow.setEnabled(bool(chosen))
        self.put_back.setEnabled(any(not found for _e, found in chosen))
        text = []
        for e, _found in chosen[:20]:
            if e.get("whole"):
                text += [f"{e['file']}  (whole new file, listed in .git/info/exclude)", ""]
                continue
            old, new, number = _changed(e)
            text.append(f"{e['file']}  (line {number})")
            text += [f"- {t}" for t in old] + [f"+ {t}" for t in new] + [""]
        self.preview.setPlainText("\n".join(text))

    def _put_back(self):
        lost = [e for e, found in self._chosen() if not found]
        try:
            left = never_commit.reapply(self.path, lost)
        except OSError as exc:
            ErrorDialog(explain(str(exc), "Put back"), self.name, self).exec()
            return
        if left:
            QMessageBox.information(self, "Put back", f"{left} entr{'ies' if left != 1 else 'y'} could not be placed: "
                                    "the lines around them changed too much. Copy them from the preview, or "
                                    "allow committing to forget them.")
        self.load()

    def _allow(self):
        chosen = [e for e, _found in self._chosen() if not e.get("whole")]
        whole = [e["file"] for e, _found in self._chosen() if e.get("whole")]
        lost = sum(1 for _e, found in self._chosen() if not found)
        if lost:
            box = QMessageBox(QMessageBox.Warning, "Allow committing",
                              f"{lost} selected entr{'ies' if lost != 1 else 'y'} no longer match their file: "
                              "forgetting them loses those lines for good.", QMessageBox.Cancel, self)
            confirm = box.addButton("Forget", QMessageBox.DestructiveRole)
            box.exec()
            if box.clickedButton() is not confirm:
                return
        if chosen:
            never_commit.forget(self.path, chosen)
        if whole:
            never_commit.forget_files(self.path, whole)
        self.load()
