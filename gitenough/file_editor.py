"""Edit a working-tree file in place, from the Changes window: encoding, BOM and line endings are kept."""

import codecs
import difflib
import os

from PySide6.QtCore import QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPolygonF, QShortcut, QSyntaxHighlighter, QTextCursor, QTextFormat
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTextEdit,
                               QToolTip, QVBoxLayout, QWidget)

from .diff_view import GUTTER_BG, MATCH_BG, MATCH_CURRENT_BG, TEXT_BG, lexer_for, mono_font, token_format
from .git_ops import GitError, run_git_bytes
from .style import C
from .widgets import ElidedLabel, icon_button

ADDED_BG = QColor("#0f2a1e")  # same tints as the diff view
CHANGED_BG = QColor("#12233a")
MARK = {"add": QColor(C["green"]), "mod": QColor(C["blue"])}

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


def reference(repo: str, rel: str, form: dict) -> str | None:
    """The staged version of the file (what the Changes diff compares with), as text; None when untracked."""
    try:
        raw = run_git_bytes(["show", f":{rel}"], cwd=repo, timeout=30)
    except GitError:
        return None
    for mark, _enc in BOMS:
        if raw.startswith(mark):
            raw = raw[len(mark):]
            break
    return raw.decode(form["encoding"], errors="replace").replace("\r\n", "\n")


def compare(old: str | None, new: str) -> tuple[dict[int, str], dict[int, list[str]]]:
    """({line index: "add" | "mod"}, {line index: lines removed just above it}) of new against old."""
    new_lines = new.split("\n")
    if old is None:
        return {i: "add" for i in range(len(new_lines))}, {}
    old_lines = old.split("\n")
    marks, removed = {}, {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "insert":
            marks.update({j: "add" for j in range(j1, j2)})
        elif tag == "replace":
            marks.update({j: "mod" for j in range(j1, j2)})
            if i2 - i1 > j2 - j1:
                removed[j2] = old_lines[i1 + j2 - j1:i2]
        elif tag == "delete":
            removed[j1] = old_lines[i1:i2]
    return marks, removed


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
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(self.editor.numbers_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_numbers(event)

    def mouseMoveEvent(self, event):
        block = self.editor.cursorForPosition(QPointF(0, event.position().y()).toPoint()).block()
        lines = self.editor.removed.get(block.blockNumber())
        if lines:
            shown = "\n".join(f"- {line}" for line in lines[:20]) + ("\n…" if len(lines) > 20 else "")
            QToolTip.showText(event.globalPosition().toPoint(), f"Removed above this line:\n{shown}", self)
        else:
            QToolTip.hideText()


class _Editor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setObjectName("diff")
        self.setFont(mono_font())
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setStyleSheet(f"QPlainTextEdit {{ background: {TEXT_BG}; }}")
        self.marks: dict[int, str] = {}  # line index -> "add" | "mod", against the staged version
        self.removed: dict[int, list[str]] = {}  # line index -> lines removed just above it
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
                n = block.blockNumber()
                if n in self.marks:
                    p.fillRect(QRect(0, int(top), 3, int(height)), MARK[self.marks[n]])
                if n in self.removed:  # a red wedge between this line and the one above
                    p.setBrush(QColor(C["red"]))
                    p.setPen(Qt.NoPen)
                    p.drawPolygon(QPolygonF([QPointF(0, top - 4), QPointF(7, top), QPointF(0, top + 4)]))
                    p.setPen(QColor(C["faint"]))
                p.drawText(QRect(0, int(top), self.numbers.width() - 8, int(height)), Qt.AlignRight | Qt.AlignVCenter,
                           str(n + 1))
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

        tools = QWidget()
        tools.setObjectName("diffHeader")
        tl = QHBoxLayout(tools)
        tl.setContentsMargins(14, 0, 10, 8)
        tl.setSpacing(8)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find in file  (Ctrl+F)")
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.setFixedWidth(220)
        self.find_edit.textChanged.connect(self._run_find)
        self.find_edit.returnPressed.connect(lambda: self._step_find(1))
        self.find_count = QLabel("")
        self.find_count.setObjectName("muted")
        self.find_count.setMinimumWidth(48)
        tl.addWidget(self.find_edit)
        tl.addWidget(self.find_count)
        for name, tip, step in (("chev_up", "Previous match (Shift+Enter)", -1), ("chev_down", "Next match (Enter)", 1)):
            btn = icon_button(name, tip, 16)
            btn.clicked.connect(lambda _=False, d=step: self._step_find(d))
            tl.addWidget(btn)
        tl.addStretch()
        self.changes_label = QLabel("")
        self.changes_label.setObjectName("muted")
        self.changes_label.setTextFormat(Qt.RichText)
        tl.addWidget(self.changes_label)
        for text, tip, step in (("↑ Change", "Previous changed line (Alt+Up)", -1),
                                ("↓ Change", "Next changed line (Alt+Down)", 1)):
            btn = QPushButton(text)
            btn.setObjectName("rowAction")
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, d=step: self._step_change(d))
            tl.addWidget(btn)
        lay.addWidget(tools)

        self.edit = _Editor()
        self.edit.document().modificationChanged.connect(self._on_modified)
        lay.addWidget(self.edit, 1)
        self.base: str | None = None  # the staged version the lines are compared with
        self.matches: list[QTextCursor] = []
        self.match_index = -1
        self.diff_timer = QTimer(self, singleShot=True, interval=300)
        self.diff_timer.timeout.connect(self._update_marks)
        self.edit.textChanged.connect(self.diff_timer.start)
        self.edit.textChanged.connect(self._refind)
        for keys, fn in (("Ctrl+S", self.save), ("Escape", self._escape), ("Ctrl+F", self._focus_find),
                         ("F3", lambda: self._step_find(1)), ("Shift+F3", lambda: self._step_find(-1)),
                         ("Alt+Down", lambda: self._step_change(1)), ("Alt+Up", lambda: self._step_change(-1))):
            QShortcut(QKeySequence(keys), self, fn).setContext(Qt.WidgetWithChildrenShortcut)
        back_find = QShortcut(QKeySequence("Shift+Return"), self.find_edit, lambda: self._step_find(-1))
        back_find.setContext(Qt.WidgetShortcut)

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
        self.base = reference(repo, rel, form)
        self.edit.setPlainText(text)
        self._update_marks()
        self.highlighter = _Highlighter(self.edit.document(), lexer_for(rel.rsplit("/", 1)[-1])
                                        if len(text) < 1_000_000 else None)
        self.edit.document().setModified(False)
        self._on_modified(False)
        self.edit.setFocus()
        return ""

    # ---------- changes against the staged version ----------
    def _update_marks(self):
        self.edit.marks, self.edit.removed = compare(self.base, self.edit.toPlainText())
        added = sum(1 for m in self.edit.marks.values() if m == "add")
        changed = len(self.edit.marks) - added
        gone = sum(len(v) for v in self.edit.removed.values())
        bits = [f"<span style='color:{C['green']}'>+{added}</span>" if added else "",
                f"<span style='color:{C['blue']}'>~{changed}</span>" if changed else "",
                f"<span style='color:{C['red']}'>−{gone}</span>" if gone else ""]
        label = " ".join(b for b in bits if b)
        self.changes_label.setText((label + " vs staged") if label else "no change vs staged")
        self._paint()
        self.edit.numbers.update()

    def _change_starts(self) -> list[int]:
        """First line of each block of changed lines, and lines with removals above them."""
        lines = sorted(set(self.edit.marks) | set(self.edit.removed))
        return [n for i, n in enumerate(lines) if i == 0 or lines[i - 1] != n - 1]

    def _step_change(self, delta: int):
        starts = self._change_starts()
        if not starts:
            return
        here = self.edit.textCursor().blockNumber()
        after = [n for n in starts if (n > here if delta > 0 else n < here)]
        target = (after[0] if delta > 0 else after[-1]) if after else (starts[0] if delta > 0 else starts[-1])
        cursor = QTextCursor(self.edit.document().findBlockByNumber(min(target, self.edit.blockCount() - 1)))
        self.edit.setTextCursor(cursor)
        self.edit.centerCursor()

    # ---------- find ----------
    def _focus_find(self):
        selected = self.edit.textCursor().selectedText()
        if selected and "\u2029" not in selected:
            self.find_edit.setText(selected)
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def _escape(self):
        if self.find_edit.hasFocus() or self.find_edit.text():
            self.find_edit.clear()
            self.edit.setFocus()
        else:
            self.close_editor()

    def _refind(self):
        if self.find_edit.text():
            self._run_find(keep=True)

    def _run_find(self, _text=None, keep: bool = False):
        term = self.find_edit.text()
        old = self.match_index
        self.matches = []
        if term:
            doc, cursor = self.edit.document(), QTextCursor(self.edit.document())
            while len(self.matches) < 5000:
                cursor = doc.find(term, cursor)
                if cursor.isNull():
                    break
                self.matches.append(cursor)
        if not self.matches:
            self.match_index = -1
        elif keep:
            self.match_index = min(max(old, 0), len(self.matches) - 1)
        else:
            # The first match from where the cursor is.
            pos = self.edit.textCursor().selectionStart()
            self.match_index = next((i for i, c in enumerate(self.matches) if c.selectionStart() >= pos), 0)
        self.find_count.setText(f"{self.match_index + 1} / {len(self.matches)}" if self.matches
                                else ("no match" if term else ""))
        self._paint()
        if self.matches and not keep:
            self._show_match()

    def _step_find(self, delta: int):
        if not self.matches:
            self._run_find()
            return
        self.match_index = (self.match_index + delta) % len(self.matches)
        self.find_count.setText(f"{self.match_index + 1} / {len(self.matches)}")
        self._paint()
        self._show_match()

    def _show_match(self):
        cursor = QTextCursor(self.matches[self.match_index])
        cursor.setPosition(cursor.selectionStart())  # caret at the match, nothing selected: typing stays safe
        self.edit.setTextCursor(cursor)
        self.edit.centerCursor()

    def _paint(self):
        """Changed lines tinted like the diff, then the find matches on top."""
        selections = []
        doc = self.edit.document()
        for n, kind in self.edit.marks.items():
            block = doc.findBlockByNumber(n)
            if not block.isValid():
                continue
            sel = QTextEdit.ExtraSelection()
            sel.cursor = QTextCursor(block)
            sel.format.setBackground(ADDED_BG if kind == "add" else CHANGED_BG)
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            selections.append(sel)
        for i, cursor in enumerate(self.matches):
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format.setBackground(MATCH_CURRENT_BG if i == self.match_index else MATCH_BG)
            if i == self.match_index:
                sel.format.setForeground(QColor("#0b0d12"))
            selections.append(sel)
        self.edit.setExtraSelections(selections)

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
