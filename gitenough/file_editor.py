"""Edit a working-tree file in place, from the Changes window: encoding, BOM and line endings are kept."""

import codecs
import os

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut, QSyntaxHighlighter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from .diff_view import GUTTER_BG, TEXT_BG, lexer_for, mono_font, token_format
from .style import C
from .widgets import ElidedLabel

MAX_EDIT_BYTES = 5_000_000
BOMS = [(codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")]


def load(path: str) -> tuple[str, dict]:
    """Text of a file (newlines as \\n) and what is needed to write it back byte for byte."""
    with open(path, "rb") as fh:
        raw = fh.read()
    bom, encoding = b"", "utf-8"
    for mark, enc in BOMS:
        if raw.startswith(mark):
            bom, encoding, raw = mark, enc, raw[len(mark):]
            break
    if encoding == "utf-8" and b"\0" in raw[:8000]:
        raise ValueError("Binary file: it cannot be edited here")
    try:
        text = raw.decode(encoding)
        errors = "strict"
    except UnicodeDecodeError:
        # Not valid UTF-8 (legacy code page): undecodable bytes survive the round trip untouched.
        text, errors = raw.decode("utf-8", errors="surrogateescape"), "surrogateescape"
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), {"bom": bom, "encoding": encoding, "errors": errors, "newline": newline}


def save(path: str, text: str, form: dict) -> None:
    data = form["bom"] + text.replace("\n", form["newline"]).encode(form["encoding"], errors=form["errors"])
    tmp = path + ".gitenough-save"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)  # never a half-written file


class _Highlighter(QSyntaxHighlighter):
    def __init__(self, doc, lexer):
        super().__init__(doc)
        self.lexer = lexer

    def highlightBlock(self, text: str):
        if self.lexer is None or len(text) > 2000:
            return
        pos = 0
        for ttype, value in self.lexer.get_tokens(text):
            if pos >= len(text):
                break
            fmt = token_format(ttype)
            if fmt is not None:
                self.setFormat(pos, min(len(value), len(text) - pos), fmt)
            pos += len(value)


class _Numbers(QWidget):
    def __init__(self, editor: "_Editor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.numbers_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_numbers(event)


class _Editor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setObjectName("diff")
        self.setFont(mono_font())
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setStyleSheet(f"QPlainTextEdit {{ background: {TEXT_BG}; }}")
        self.numbers = _Numbers(self)
        self.blockCountChanged.connect(lambda _n: self.setViewportMargins(self.numbers_width(), 0, 0, 0))
        self.updateRequest.connect(self._on_update)
        self.setViewportMargins(self.numbers_width(), 0, 0, 0)

    def numbers_width(self) -> int:
        return 16 + self.fontMetrics().horizontalAdvance("9") * max(3, len(str(self.blockCount())))

    def _on_update(self, rect, dy):
        if dy:
            self.numbers.scroll(0, dy)
        else:
            self.numbers.update(0, rect.y(), self.numbers.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.numbers.setGeometry(QRect(cr.left(), cr.top(), self.numbers_width(), cr.height()))

    def paint_numbers(self, event):
        p = QPainter(self.numbers)
        p.fillRect(event.rect(), QColor(GUTTER_BG))
        p.setPen(QColor(C["faint"]))
        p.setFont(self.font())
        block = self.firstVisibleBlock()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        while block.isValid() and top <= event.rect().bottom():
            height = self.blockBoundingRect(block).height()
            if block.isVisible() and top + height >= event.rect().top():
                p.drawText(QRect(0, int(top), self.numbers.width() - 8, int(height)), Qt.AlignRight | Qt.AlignVCenter,
                           str(block.blockNumber() + 1))
            block = block.next()
            top += height


class FileEditor(QWidget):
    """The whole file, editable; Save writes it, Back returns to the diff."""

    saved = Signal(str)  # path relative to the repository
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.repo = self.rel = self.full = ""
        self.form: dict = {}
        self.mtime = 0.0
        self.highlighter = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        header = QWidget()
        header.setObjectName("diffHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 8, 10, 8)
        hl.setSpacing(8)
        self.title = ElidedLabel("", Qt.ElideLeft)
        self.title.setStyleSheet("font-weight: 700; font-size: 10.5pt;")
        self.state = QLabel("")
        self.state.setObjectName("muted")
        back = QPushButton("Back to diff")
        back.setToolTip("Show the diff (Esc)")
        back.clicked.connect(self.close_editor)
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("primary")
        self.save_btn.setToolTip("Write the file (Ctrl+S); the changes are evaluated again right away")
        self.save_btn.clicked.connect(self.save)
        hl.addWidget(self.title, 1)
        hl.addWidget(self.state)
        hl.addWidget(back)
        hl.addWidget(self.save_btn)
        lay.addWidget(header)
        self.edit = _Editor()
        self.edit.document().modificationChanged.connect(self._on_modified)
        lay.addWidget(self.edit, 1)
        for keys, fn in (("Ctrl+S", self.save), ("Escape", self.close_editor)):
            QShortcut(QKeySequence(keys), self, fn).setContext(Qt.WidgetWithChildrenShortcut)

    @property
    def modified(self) -> bool:
        return self.edit.document().isModified()

    def open_file(self, repo: str, rel: str) -> str:
        """Load a file; returns an error message, or "" when it is open."""
        full = os.path.join(repo, rel)
        if not os.path.isfile(full):
            return "The file does not exist in the working folder (deleted)."
        if os.path.getsize(full) > MAX_EDIT_BYTES:
            return f"Files over {MAX_EDIT_BYTES // 1_000_000} MB are not edited here: open it in your editor."
        try:
            text, form = load(full)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return str(exc)
        self.repo, self.rel, self.full, self.form = repo, rel, full, form
        self.mtime = os.path.getmtime(full)
        self.title.setText(rel)
        self.edit.setPlainText(text)
        self.highlighter = _Highlighter(self.edit.document(), lexer_for(rel.rsplit("/", 1)[-1])
                                        if len(text) < 1_000_000 else None)
        self.edit.document().setModified(False)
        self._on_modified(False)
        self.edit.setFocus()
        return ""

    def _on_modified(self, modified: bool):
        eol = "CRLF" if self.form.get("newline") == "\r\n" else "LF"
        enc = self.form.get("encoding", "utf-8").upper() + (" BOM" if self.form.get("bom") else "")
        self.state.setText(("● modified  ·  " if modified else "") + f"{enc} · {eol}")
        self.save_btn.setEnabled(modified)

    def save(self) -> bool:
        if not self.full or not self.modified:
            return True
        if os.path.exists(self.full) and os.path.getmtime(self.full) != self.mtime:
            box = QMessageBox(QMessageBox.Warning, "Save", f"{self.rel} changed on disk since it was opened here.",
                              QMessageBox.Cancel, self)
            overwrite = box.addButton("Overwrite it", QMessageBox.DestructiveRole)
            box.exec()
            if box.clickedButton() is not overwrite:
                return False
        try:
            save(self.full, self.edit.toPlainText(), self.form)
        except (OSError, UnicodeEncodeError) as exc:
            QMessageBox.critical(self, "Save", f"Could not write {self.rel}:\n{exc}")
            return False
        self.mtime = os.path.getmtime(self.full)
        self.edit.document().setModified(False)
        self.saved.emit(self.rel)
        return True

    def confirm_leave(self) -> bool:
        """Before leaving the editor: True when there is nothing unsaved (saved, discarded or clean)."""
        if not self.modified:
            return True
        box = QMessageBox(QMessageBox.Question, "Unsaved changes", f"Save the changes to {self.rel}?",
                          QMessageBox.Cancel, self)
        save_btn = box.addButton("Save", QMessageBox.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.DestructiveRole)
        box.setDefaultButton(save_btn)
        box.exec()
        if box.clickedButton() is save_btn:
            return self.save()
        if box.clickedButton() is discard:
            self.edit.document().setModified(False)
            return True
        return False

    def close_editor(self):
        if self.confirm_leave():
            self.closed.emit()

    def reload_if_clean(self):
        """The file changed on disk (other editor, git): follow it unless there are unsaved edits here."""
        if self.full and not self.modified and os.path.exists(self.full) and \
                os.path.getmtime(self.full) != self.mtime:
            scroll = self.edit.verticalScrollBar().value()
            self.open_file(self.repo, self.rel)
            self.edit.verticalScrollBar().setValue(scroll)
