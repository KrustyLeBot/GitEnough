import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

from pygments.lexers import get_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QFontMetrics, QKeySequence, QPainter, QPen,
                           QShortcut, QSyntaxHighlighter, QTextBlockFormat, QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QSplitter, QStackedWidget, QTextEdit, QVBoxLayout, QWidget)

from .style import C
from .widgets import ElidedLabel, icon_button

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$")
TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")
MAX_LINES = 30000
MAX_LINE_CHARS = 800  # characters of a line shown and highlighted (a screen shows ~150)

TEXT_BG = "#0f1116"
GUTTER_BG = "#12141a"
LINE_BG = {"add": QColor("#0f2a1e"), "del": QColor("#2d151a"), "hunk": QColor("#141a2e")}
GUTTER_TINT = {"add": QColor("#133524"), "del": QColor("#3a1a20"), "hunk": QColor("#161d33")}
WORD_BG = {"add": QColor("#1f6040"), "del": QColor("#74303a")}
FILL_BRUSH = QBrush(QColor("#1f232c"), Qt.BDiagPattern)
MATCH_BG = QColor("#5a4a16")
MATCH_CURRENT_BG = QColor("#c08a1e")
HUNK_LABELS = {"stage": "Stage hunk", "unstage": "Unstage hunk", "discard": "Discard hunk"}
LINE_LABELS = {"stage": "Stage lines", "unstage": "Unstage lines", "discard": "Discard lines"}


@dataclass(slots=True)
class Line:
    kind: str  # "hunk" | "ctx" | "add" | "del" | "meta" | "fill"
    old: int | None
    new: int | None
    text: str
    spans: list = field(default_factory=list)  # changed character ranges (word diff)
    hunk: int = -1
    idx: int = -1  # index in the hunk body


FILL = Line("fill", None, None, "")
SLOT = Line("slot", None, None, "")  # empty row reserved for an inline widget (comment threads)


@dataclass
class DiffData:
    lines: list[Line] = field(default_factory=list)
    binary: bool = False
    added: int = 0
    removed: int = 0
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    hunk_sizes: list[int] = field(default_factory=list)


def parse_diff(text: str) -> DiffData:
    data = DiffData()
    old = new = 0
    in_hunk = False
    old_mode = ""
    for raw in text.split("\n"):
        if raw.startswith("diff --git") or raw.startswith("diff --cc"):
            in_hunk = False
            continue
        if raw.startswith("@@"):
            m = HUNK_RE.match(raw)
            if not m:  # Combined (merge conflict) diff: show it verbatim.
                data.lines.append(Line("meta", None, None, raw))
                in_hunk = False
                continue
            old, new, in_hunk = int(m[1]), int(m[2]), True
            data.hunk_sizes.append(0)
            data.lines.append(Line("hunk", None, None, raw, hunk=len(data.hunk_sizes) - 1))
            continue
        if not in_hunk:
            if raw.startswith(("Binary files", "GIT binary patch")):
                data.binary = True
            elif raw.startswith("new file mode"):
                data.notes.append("New file")
            elif raw.startswith("deleted file mode"):
                data.notes.append("Deleted file")
            elif raw.startswith("rename from "):
                data.notes.append(f"Renamed from {raw[12:]}")
            elif raw.startswith("old mode "):
                old_mode = raw[9:]
            elif raw.startswith("new mode "):
                data.notes.append(f"Mode {old_mode} → {raw[9:]}")
            elif raw.startswith("truncated "):
                data.notes.append("Large file: only the first 2 MB are shown")
            elif data.lines and raw and not raw.startswith(("index ", "--- ", "+++ ", "similarity", "rename to")):
                data.lines.append(Line("meta", None, None, raw))
            continue
        if len(data.lines) >= MAX_LINES:
            data.truncated = True
            break
        tag = raw[:1]
        if tag == "+":
            line = Line("add", None, new, raw[1:])
            new += 1
            data.added += 1
        elif tag == "-":
            line = Line("del", old, None, raw[1:])
            old += 1
            data.removed += 1
        elif tag == " ":
            line = Line("ctx", old, new, raw[1:])
            old += 1
            new += 1
        elif tag == "\\":
            line = Line("meta", None, None, raw[2:])
        else:
            continue
        # Position inside the hunk body, used to rebuild partial patches from the raw diff.
        line.hunk, line.idx = len(data.hunk_sizes) - 1, data.hunk_sizes[-1]
        data.hunk_sizes[-1] += 1
        data.lines.append(line)
    _word_diff(data.lines)
    return data


def _offsets(tokens: list[str]) -> list[int]:
    out = [0]
    for t in tokens:
        out.append(out[-1] + len(t))
    return out


def _spans(a: str, b: str):
    if len(a) > 500 or len(b) > 500:
        return None
    ta, tb = TOKEN_RE.findall(a), TOKEN_RE.findall(b)
    sm = SequenceMatcher(None, ta, tb, autojunk=False)
    # Mostly different lines read better without word highlights.
    if sm.ratio() < 0.4:
        return None
    pa, pb = _offsets(ta), _offsets(tb)
    sa, sb = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            sa.append((pa[i1], pa[i2]))
        if j2 > j1:
            sb.append((pb[j1], pb[j2]))
    return sa, sb


def _word_diff(lines: list[Line]) -> None:
    i, n = 0, len(lines)
    while i < n:
        if lines[i].kind != "del":
            i += 1
            continue
        j = i
        while j < n and lines[j].kind == "del":
            j += 1
        k = j
        while k < n and lines[k].kind == "add":
            k += 1
        for a, b in zip(lines[i:j], lines[j:k]):
            spans = _spans(a.text, b.text)
            if spans:
                a.spans, b.spans = spans
        i = max(k, j)


def split_lines(lines: list[Line]) -> tuple[list[Line], list[Line]]:
    left, right = [], []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.kind in ("add", "del"):
            dels, adds = [], []
            while i < n and lines[i].kind == "del":
                dels.append(lines[i])
                i += 1
            while i < n and lines[i].kind == "add":
                adds.append(lines[i])
                i += 1
            for k in range(max(len(dels), len(adds))):
                left.append(dels[k] if k < len(dels) else FILL)
                right.append(adds[k] if k < len(adds) else FILL)
        else:
            left.append(line)
            right.append(line)
            i += 1
    return left, right


# ---------- syntax highlighting ----------

def _fmt(color: str, italic: bool = False, bold: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    f.setFontItalic(italic)
    if bold:
        f.setFontWeight(QFont.DemiBold)
    return f


TOKEN_STYLES = [
    (Token.Comment, _fmt("#6b7386", italic=True)),
    (Token.Literal.String.Doc, _fmt("#6b7386", italic=True)),
    (Token.Keyword.Type, _fmt("#ffcb6b")),
    (Token.Keyword, _fmt("#c792ea")),
    (Token.Operator.Word, _fmt("#c792ea")),
    (Token.Name.Builtin, _fmt("#f78c6c")),
    (Token.Name.Function, _fmt("#82aaff")),
    (Token.Name.Class, _fmt("#ffcb6b")),
    (Token.Name.Decorator, _fmt("#ffcb6b")),
    (Token.Name.Tag, _fmt("#f07178")),
    (Token.Name.Attribute, _fmt("#ffcb6b")),
    (Token.Name.Namespace, _fmt("#b2ccd6")),
    (Token.Name.Constant, _fmt("#f78c6c")),
    (Token.Name.Variable, _fmt("#f07178")),
    (Token.Literal.String, _fmt("#c3e88d")),
    (Token.Literal.Number, _fmt("#f78c6c")),
    (Token.Operator, _fmt("#89ddff")),
    (Token.Punctuation, _fmt("#9aa5ba")),
    (Token.Generic.Heading, _fmt("#82aaff", bold=True)),
    (Token.Generic.Subheading, _fmt("#82aaff", bold=True)),
]
HUNK_FMT = _fmt("#7f8ccc")
META_FMT = _fmt(C["faint"], italic=True)


@lru_cache(maxsize=512)
def token_format(ttype) -> QTextCharFormat | None:
    for base, fmt in TOKEN_STYLES:
        if ttype in base:
            return fmt
    return None


def warm_up_lexers() -> None:
    # The first lookup loads pygments' lexer index (~150 ms): pay it in the background at startup.
    for name in ("x.cs", "x.py", "x.ts", "x.js", "x.json", "x.xml", "x.md", "x.yml"):
        lexer_for(name)


@lru_cache(maxsize=128)
def lexer_for(filename: str):
    try:
        return get_lexer_for_filename(filename, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


class DiffHighlighter(QSyntaxHighlighter):
    def __init__(self, editor: "DiffEditor"):
        super().__init__(None)
        self.editor = editor
        self.lexer = None

    def highlightBlock(self, text: str):
        n = self.currentBlock().blockNumber()
        lines = self.editor.lines
        if n >= len(lines):
            return
        line = lines[n]
        if line.kind == "hunk":
            self.setFormat(0, len(text), HUNK_FMT)
            return
        if line.kind == "meta":
            self.setFormat(0, len(text), META_FMT)
            return
        if line.kind in ("fill", "slot"):
            return
        size = len(text)
        tokens = []  # (start, length, format or None)
        if self.lexer is not None and size <= MAX_LINE_CHARS:
            pos = 0
            # Lines are lexed one by one: multi-line constructs lose context, which is fine for a diff.
            for ttype, value in self.lexer.get_tokens(text):
                if pos >= size:
                    break
                tokens.append((pos, min(len(value), size - pos), token_format(ttype)))
                pos += len(value)
        spans = [(a, min(b, size)) for a, b in line.spans if a < size]
        if not spans:
            for start, length, fmt in tokens:
                if fmt is not None:
                    self.setFormat(start, length, fmt)
            return
        # Word-diff background merged into the token formats, range by range (not character by character).
        bg = WORD_BG.get(line.kind)
        cuts = sorted({0, size, *(x for sp in spans for x in sp)})
        tokens = tokens or [(0, size, None)]
        for start, length, fmt in tokens:
            end = start + length
            pieces = [start] + [c for c in cuts if start < c < end] + [end]
            for a, b in zip(pieces, pieces[1:]):
                inside = any(sa <= a < sb for sa, sb in spans)
                if not inside and fmt is None:
                    continue
                f = QTextCharFormat(fmt) if fmt is not None else QTextCharFormat()
                if inside:
                    f.setBackground(bg)
                self.setFormat(a, b - a, f)


# ---------- editor ----------

def mono_font() -> QFont:
    families = set(QFontDatabase.families())
    font = QFont(next((f for f in ("Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Consolas")
                       if f in families), "Consolas"))
    font.setStyleHint(QFont.Monospace)
    font.setPointSizeF(9.5)
    return font


class _Gutter(QWidget):
    def __init__(self, editor: "DiffEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_gutter(event)

    def mouseMoveEvent(self, event):
        self.editor.gutter_hover(event.position().toPoint())

    def leaveEvent(self, event):
        self.editor.gutter_hover(None)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.editor.gutter_click(event.position().toPoint())


class DiffEditor(QPlainTextEdit):
    """Read-only diff pane. mode: "unified" (two number columns), "old" or "new" (split sides)."""

    hunk_action = Signal(int, str)  # hunk index, action
    line_clicked = Signal(int)  # index in self.lines of a line number clicked (comment mode)

    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode
        self.lines: list[Line] = []
        self.max_no = 0
        self.actions: list[str] = []  # buttons drawn on hunk headers: "stage", "unstage", "discard"
        self.commentable = False  # line numbers clickable to comment (merge request review)
        self.slots: list[tuple[int, int, QWidget]] = []  # (first row, row count, widget shown over them)
        self.markers: dict[int, tuple[int, str]] = {}  # line index -> (comment count, color)
        self._gutter_hover = -1
        self._hover = None
        self._btn_font = QFont()
        self._btn_font.setPointSizeF(8.5)
        self._btn_font.setBold(True)
        self.viewport().setMouseTracking(True)
        self.setObjectName("diff")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(mono_font())
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.document().setDocumentMargin(2)
        self.setCursorWidth(0)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.gutter = _Gutter(self)
        self.highlighter = DiffHighlighter(self)
        self.updateRequest.connect(self._on_update)
        self._update_margins()

    def gutter_width(self) -> int:
        digits = max(3, len(str(self.max_no)))
        cw = self.fontMetrics().horizontalAdvance("9")
        cols = digits * 2 + 1 if self.mode == "unified" else digits
        return 14 + cols * cw + 22 + (self.MARK_W if self.commentable else 0)

    MARK_W = 22

    def _gutter_line(self, pos) -> int:
        if pos is None:
            return -1
        block = self.cursorForPosition(QPoint(0, pos.y())).block()
        n = block.blockNumber()
        if n >= len(self.lines) or self.lines[n].kind not in ("add", "del", "ctx"):
            return -1
        return n

    def gutter_hover(self, pos):
        n = self._gutter_line(pos) if self.commentable else -1
        if n != self._gutter_hover:
            self._gutter_hover = n
            self.gutter.setCursor(Qt.PointingHandCursor if n >= 0 else Qt.ArrowCursor)
            self.gutter.setToolTip("Comment on this line" if n >= 0 else "")
            self.gutter.update()

    def gutter_click(self, pos):
        n = self._gutter_line(pos) if self.commentable else -1
        if n >= 0:
            self.line_clicked.emit(n)

    def set_markers(self, markers: dict[int, tuple[int, str]]):
        self.markers = markers
        self.gutter.update()

    def _update_margins(self):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _place_gutter(self):
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_gutter()

    def _on_update(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if self.slots:
            self._place_slots()

    # ---------- inline widgets ----------
    SLOT_X = 14
    SLOT_MAX_W = 1000

    def slot_width(self) -> int:
        return max(320, min(self.viewport().width() - 2 * self.SLOT_X, self.SLOT_MAX_W))

    def rows_for(self, widget: QWidget) -> int:
        """Empty rows to reserve so a widget fits under its line at the current width."""
        widget.setParent(self.viewport())
        width = self.slot_width()
        widget.setFixedWidth(width)
        lay = widget.layout()
        if lay is not None:
            lay.activate()
            height = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        else:
            height = widget.sizeHint().height()
        return max(1, math.ceil((height + 10) / max(1, self.fontMetrics().lineSpacing())))

    def set_slots(self, slots: list[tuple[int, int, QWidget]]):
        self.slots = slots
        for _start, _count, w in slots:
            w.setParent(self.viewport())
        self._place_slots()

    def _place_slots(self):
        offset = self.contentOffset()
        doc = self.document()
        for start, count, w in self.slots:
            if w.parent() is not self.viewport():
                continue  # moved to the other view, or removed: showing it would open a stray window
            first, last = doc.findBlockByNumber(start), doc.findBlockByNumber(start + count - 1)
            if not first.isValid() or not last.isValid():
                continue
            top = self.blockBoundingGeometry(first).translated(offset).top()
            bottom = self.blockBoundingGeometry(last).translated(offset).bottom()
            w.setGeometry(self.SLOT_X, int(top) + 4, self.slot_width(), max(8, int(bottom - top) - 8))
            w.show()

    def set_lines(self, lines: list[Line], lexer):
        self.lines = lines
        self.max_no = max((max(l.old or 0, l.new or 0) for l in lines), default=0)
        self.highlighter.setDocument(None)
        self.highlighter.lexer = lexer
        # Very long lines (minified or generated files) are cut on screen: laying them out costs seconds.
        self.setPlainText("\n".join(l.text if len(l.text) <= MAX_LINE_CHARS else
                                    f"{l.text[:MAX_LINE_CHARS]}  … {len(l.text) - MAX_LINE_CHARS} more characters"
                                    for l in lines))
        doc = self.document()
        block = doc.firstBlock()
        for line in lines:
            if not block.isValid():
                break
            if line.kind == "fill":
                fmt = QTextBlockFormat()
                fmt.setBackground(FILL_BRUSH)
                QTextCursor(block).setBlockFormat(fmt)
            elif line.kind in LINE_BG:
                fmt = QTextBlockFormat()
                fmt.setBackground(LINE_BG[line.kind])
                QTextCursor(block).setBlockFormat(fmt)
            block = block.next()
        self.highlighter.setDocument(doc)
        self._update_margins()
        self._place_gutter()
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)
        self.slots = []

    def paint_gutter(self, event):
        p = QPainter(self.gutter)
        p.fillRect(event.rect(), QColor(GUTTER_BG))
        fm = self.fontMetrics()
        cw = fm.horizontalAdvance("9")
        digits = max(3, len(str(self.max_no)))
        width = self.gutter.width()
        block = self.firstVisibleBlock()
        n = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        p.setFont(self.font())
        while block.isValid() and top <= event.rect().bottom():
            if bottom >= event.rect().top() and n < len(self.lines):
                line = self.lines[n]
                h = int(bottom - top)
                row = QRect(0, int(top), width, h)
                if line.kind == "fill":
                    p.fillRect(row, FILL_BRUSH)
                elif line.kind in GUTTER_TINT:
                    p.fillRect(row, GUTTER_TINT[line.kind])
                numbers = []
                if self.mode in ("unified", "old"):
                    numbers.append(line.old)
                if self.mode in ("unified", "new"):
                    numbers.append(line.new)
                x = 8
                if self.commentable:
                    mark = self.markers.get(n)
                    if mark or n == self._gutter_hover:
                        self._paint_mark(p, QRectF(4, top + (h - 15) / 2, 16, 15), mark)
                    x += self.MARK_W
                p.setPen(QColor(C["faint"]))
                for num in numbers:
                    if num is not None:
                        p.drawText(QRect(x, int(top), digits * cw, h), Qt.AlignRight | Qt.AlignVCenter, str(num))
                    x += digits * cw + cw
                sign = {"add": "+", "del": "−"}.get(line.kind)
                if sign:
                    p.setPen(QColor(C["green"] if line.kind == "add" else C["red"]))
                    p.drawText(QRect(width - 18, int(top), 14, h), Qt.AlignCenter, sign)
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            n += 1

    def _paint_mark(self, p: QPainter, rect: QRectF, mark):
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        color = QColor(mark[1] if mark else C["accent"])
        if not mark:
            color.setAlpha(150)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(rect, 4, 4)
        p.setPen(QColor("#0b0d12"))
        f = QFont(self._btn_font)
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.drawText(rect, Qt.AlignCenter, str(mark[0]) if mark else "+")
        p.restore()

    # ---------- hunk buttons & line selection ----------
    def set_actions(self, actions: list[str]):
        self.actions = actions
        self._hover = None
        self.viewport().update()

    def _hunk_buttons(self, top: float, height: float) -> list[tuple[QRectF, str]]:
        fm = QFontMetrics(self._btn_font)
        x = self.viewport().width() - 10
        out = []
        for action in reversed(self.actions):
            w = fm.horizontalAdvance(HUNK_LABELS[action]) + 20
            out.append((QRectF(x - w, top + 1.5, w, height - 3), action))
            x -= w + 6
        return out

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.actions:
            return
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(self._btn_font)
        offset = self.contentOffset()
        block = self.firstVisibleBlock()
        n = block.blockNumber()
        while block.isValid():
            geo = self.blockBoundingGeometry(block).translated(offset)
            if geo.top() > event.rect().bottom():
                break
            if n < len(self.lines) and self.lines[n].kind == "hunk":
                for rect, action in self._hunk_buttons(geo.top(), geo.height()):
                    color = QColor(C["red"] if action == "discard" else C["accent"])
                    fill = QColor(color)
                    fill.setAlpha(80 if self._hover == (n, action) else 34)
                    p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 110), 1))
                    p.setBrush(fill)
                    p.drawRoundedRect(rect, 5, 5)
                    p.setPen(color.lighter(125))
                    p.drawText(rect, Qt.AlignCenter, HUNK_LABELS[action])
            block = block.next()
            n += 1

    def _button_at(self, pos):
        if not self.actions:
            return None
        block = self.cursorForPosition(pos).block()
        n = block.blockNumber()
        if n >= len(self.lines) or self.lines[n].kind != "hunk":
            return None
        geo = self.blockBoundingGeometry(block).translated(self.contentOffset())
        for rect, action in self._hunk_buttons(geo.top(), geo.height()):
            if rect.contains(QPointF(pos)):
                return n, action
        return None

    def mouseMoveEvent(self, event):
        hit = self._button_at(event.position().toPoint())
        if hit != self._hover:
            self._hover = hit
            self.viewport().setCursor(Qt.PointingHandCursor if hit else Qt.IBeamCursor)
            self.viewport().update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        hit = self._button_at(event.position().toPoint())
        if hit and event.button() == Qt.LeftButton:
            self.hunk_action.emit(self.lines[hit[0]].hunk, hit[1])
            return
        super().mousePressEvent(event)

    def selected_changes(self) -> dict[int, set[int]]:
        """Changed lines (add/del) covered by the text selection, grouped by hunk."""
        c = self.textCursor()
        if not c.hasSelection():
            return {}
        doc = self.document()
        first = doc.findBlock(c.selectionStart()).blockNumber()
        end_block = doc.findBlock(c.selectionEnd())
        last = end_block.blockNumber()
        if c.selectionEnd() == end_block.position() and last > first:
            last -= 1  # Selection ending at the start of a line does not include that line.
        picked: dict[int, set[int]] = {}
        for n in range(first, min(last, len(self.lines) - 1) + 1):
            line = self.lines[n]
            if line.kind in ("add", "del"):
                picked.setdefault(line.hunk, set()).add(line.idx)
        return picked

    def find_all(self, term: str) -> list[QTextCursor]:
        found = []
        if not term:
            return found
        doc = self.document()
        cursor = QTextCursor(doc)
        while len(found) < 5000:
            cursor = doc.find(term, cursor)
            if cursor.isNull():
                break
            found.append(cursor)
        return found

    def show_matches(self, cursors: list[QTextCursor], current: QTextCursor | None):
        selections = []
        for c in cursors:
            sel = QTextEdit.ExtraSelection()
            sel.cursor = c
            sel.format.setBackground(MATCH_CURRENT_BG if current is not None and c == current else MATCH_BG)
            if current is not None and c == current:
                sel.format.setForeground(QColor("#0b0d12"))
            selections.append(sel)
        self.setExtraSelections(selections)


class DiffView(QWidget):
    """Header (file, stats, find, options) plus unified or side-by-side diff panes."""

    options_changed = Signal()
    patch_requested = Signal(object, str)  # {hunk: None | set(line indexes)}, action
    comment_requested = Signal(object)  # Line whose number was clicked (comment mode)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data: DiffData | None = None
        self.filename = ""
        self.actions: list[str] = []
        self._sel_editor: DiffEditor | None = None
        self.matches: list[tuple[DiffEditor, QTextCursor]] = []
        self.match_index = -1
        self.markers: dict[tuple[str, int], tuple[int, str]] = {}  # (side, line number) -> (count, color)
        # Widgets shown under a line: key ("new"|"old", number), or "top" for the file itself.
        self.inline: dict = {}
        self._inline_width = 0
        self._resize_timer = QTimer(self, singleShot=True, interval=150)
        self._resize_timer.timeout.connect(self._rewrap)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QWidget()
        header.setObjectName("diffHeader")
        rows = QVBoxLayout(header)
        rows.setContentsMargins(14, 8, 10, 8)
        rows.setSpacing(6)
        # Two rows: the file name gets the full width (elided, never widening the window), tools below.
        top = QHBoxLayout()
        top.setSpacing(10)
        self.title = ElidedLabel("", Qt.ElideLeft)
        self.title.setStyleSheet("font-weight: 700; font-size: 10.5pt;")
        self.stats = QLabel("")
        self.stats.setTextFormat(Qt.RichText)
        top.addWidget(self.title, 1)
        top.addWidget(self.stats)
        rows.addLayout(top)
        hl = QHBoxLayout()
        hl.setSpacing(8)
        rows.addLayout(hl)

        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find in diff")
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.setFixedWidth(170)
        self.find_edit.textChanged.connect(self.run_find)
        self.find_edit.returnPressed.connect(lambda: self.step_find(1))
        self.find_count = QLabel("")
        self.find_count.setObjectName("muted")
        self.find_count.setMinimumWidth(40)
        prev_btn = icon_button("chev_up", "Previous match (Shift+Enter)", 16)
        next_btn = icon_button("chev_down", "Next match (Enter)", 16)
        for b, d in ((prev_btn, -1), (next_btn, 1)):
            b.clicked.connect(lambda _=False, d=d: self.step_find(d))
        back = QShortcut(QKeySequence("Shift+Return"), self.find_edit, lambda: self.step_find(-1))
        back.setContext(Qt.WidgetShortcut)
        hl.addWidget(self.find_edit)
        hl.addWidget(self.find_count)
        hl.addWidget(prev_btn)
        hl.addWidget(next_btn)
        hl.addStretch()

        self.ws = QCheckBox("Ignore whitespace")
        self.full = QCheckBox("Full file")
        for cb in (self.ws, self.full):
            cb.toggled.connect(self.options_changed)
            hl.addWidget(cb)

        self.unified_btn = QPushButton("Unified")
        self.split_btn = QPushButton("Split")
        group = QButtonGroup(self)
        for b, name in ((self.unified_btn, "segL"), (self.split_btn, "segR")):
            b.setCheckable(True)
            b.setObjectName(name)
            group.addButton(b)
        self.split_btn.setChecked(True)
        group.buttonToggled.connect(lambda *_: self.render())
        seg = QHBoxLayout()
        seg.setSpacing(0)
        seg.addWidget(self.unified_btn)
        seg.addWidget(self.split_btn)
        hl.addSpacing(4)
        hl.addLayout(seg)
        lay.addWidget(header)

        self.notes = ElidedLabel("", Qt.ElideRight)
        self.notes.setObjectName("diffNotes")
        self.notes.hide()
        lay.addWidget(self.notes)

        self.sel_bar = QWidget()
        self.sel_bar.setObjectName("selBar")
        sl = QHBoxLayout(self.sel_bar)
        sl.setContentsMargins(14, 5, 10, 5)
        self.sel_label = QLabel("")
        sl.addWidget(self.sel_label)
        sl.addStretch()
        self.sel_buttons: dict[str, QPushButton] = {}
        for action in ("discard", "unstage", "stage"):
            btn = QPushButton(LINE_LABELS[action])
            btn.setObjectName("rowAction" if action != "discard" else "dangerSmall")
            btn.clicked.connect(lambda _=False, a=action: self._line_action(a))
            sl.addWidget(btn)
            self.sel_buttons[action] = btn
        self.sel_bar.hide()
        lay.addWidget(self.sel_bar)

        self.stack = QStackedWidget()
        self.message = QLabel("Select a file to see its diff")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setObjectName("muted")
        self.message.setStyleSheet(f"background: {TEXT_BG}; font-size: 10pt;")
        self.unified = DiffEditor("unified")
        self.left, self.right = DiffEditor("old"), DiffEditor("new")
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(1)
        split.addWidget(self.left)
        split.addWidget(self.right)
        for a, b in ((self.left, self.right), (self.right, self.left)):
            a.verticalScrollBar().valueChanged.connect(b.verticalScrollBar().setValue)
            a.horizontalScrollBar().valueChanged.connect(b.horizontalScrollBar().setValue)
        for ed in (self.unified, self.left, self.right):
            ed.hunk_action.connect(lambda hunk, action: self.patch_requested.emit({hunk: None}, action))
            ed.selectionChanged.connect(lambda ed=ed: self._on_selection(ed))
            ed.line_clicked.connect(lambda n, ed=ed: self.comment_requested.emit(ed.lines[n]))
        self.stack.addWidget(self.message)
        self.stack.addWidget(self.unified)
        self.stack.addWidget(split)
        lay.addWidget(self.stack, 1)

    @property
    def ignore_ws(self) -> bool:
        return self.ws.isChecked()

    @property
    def full_file(self) -> bool:
        return self.full.isChecked()

    def set_message(self, text: str, title: str = ""):
        self.data = None
        self.title.setText(title)
        self.stats.setText("")
        self.notes.hide()
        self.message.setText(text)
        self.stack.setCurrentIndex(0)
        self._sync_actions()
        self.run_find()

    def set_actions(self, actions: list[str]):
        """Hunk / line actions offered for the current diff ("stage", "unstage", "discard")."""
        self.actions = actions
        self._sync_actions()

    def _effective_actions(self) -> list[str]:
        d = self.data
        # Patches are rebuilt from the plain diff: impossible when whitespace is ignored or lines are cut.
        if d is None or d.binary or d.truncated or self.ignore_ws:
            return []
        return self.actions

    def _sync_actions(self):
        actions = self._effective_actions()
        self.unified.set_actions(actions)
        self.left.set_actions([])
        self.right.set_actions(actions)
        for action, btn in self.sel_buttons.items():
            btn.setVisible(action in actions)
        self._on_selection(self._sel_editor)

    def _on_selection(self, editor):
        self._sel_editor = editor
        picked = editor.selected_changes() if editor is not None and self._effective_actions() else {}
        count = sum(len(v) for v in picked.values())
        self.sel_bar.setVisible(bool(count))
        self.sel_label.setText(f"{count} changed line{'s' if count != 1 else ''} selected")

    def _line_action(self, action: str):
        if self._sel_editor is not None:
            picked = self._sel_editor.selected_changes()
            if picked:
                self.patch_requested.emit(picked, action)

    def current_scroll(self) -> int:
        eds = self.editors()
        return eds[0].verticalScrollBar().value() if eds else 0

    def set_diff(self, filename: str, data: DiffData, keep_scroll: bool = False):
        scroll = self.current_scroll() if keep_scroll and filename == self.filename else 0
        self.filename, self.data = filename, data
        self.title.setText(filename)
        self.stats.setText(f"<span style='color:{C['green']}'>+{data.added}</span>&nbsp;&nbsp;"
                           f"<span style='color:{C['red']}'>−{data.removed}</span>")
        notes = list(data.notes)
        if data.truncated:
            notes.append(f"Diff truncated to {MAX_LINES} lines")
        self.notes.setText("   ·   ".join(notes))
        self.notes.setVisible(bool(notes))
        self.render()
        if scroll:
            # Scroll range is only known once the document is laid out.
            QTimer.singleShot(0, lambda: [e.verticalScrollBar().setValue(scroll) for e in self.editors()])

    def render(self):
        data = self.data
        if data is None:
            return
        if data.binary:
            self.message.setText("Binary file: no text diff")
            self.stack.setCurrentIndex(0)
        elif not data.lines:
            self.message.setText("No textual changes" + (" (whitespace ignored)" if self.ignore_ws else ""))
            self.stack.setCurrentIndex(0)
        else:
            # Per-line lexing is the main rendering cost: skip it on very large diffs.
            chars = sum(min(len(l.text), MAX_LINE_CHARS) for l in data.lines)
            small = len(data.lines) <= 4000 and chars <= 150_000
            lexer = lexer_for(self.filename.rsplit("/", 1)[-1]) if small else None
            if self.unified_btn.isChecked():
                self.stack.setCurrentIndex(1)
                lines, slots = self._with_slots([data.lines], [self.unified])
                self.unified.set_lines(lines[0], lexer)
                self.unified.set_slots(slots[0])
            else:
                self.stack.setCurrentIndex(2)
                (left, right), (lslots, rslots) = self._with_slots(list(split_lines(data.lines)),
                                                                   [self.left, self.right])
                self.left.set_lines(left, lexer)
                self.right.set_lines(right, lexer)
                self.left.set_slots(lslots)
                self.right.set_slots(rslots)
            self._inline_width = self.width()
        self._sync_actions()
        self._apply_markers()
        self.run_find()

    def _anchor(self, line: Line, side: str | None):
        """Key of the inline widget under this line, if any."""
        if line.kind in ("add", "ctx") and side != "old" and ("new", line.new) in self.inline:
            return "new", line.new
        if line.kind in ("del", "ctx") and side != "new" and ("old", line.old) in self.inline:
            return "old", line.old
        return None

    def _with_slots(self, columns: list[list[Line]], editors: list[DiffEditor]):
        """Insert empty rows under anchored lines (same count in every column, so split sides stay aligned).

        Returns the new columns and, per editor, its (first row, rows, widget) list.
        """
        if not self.inline:
            return columns, [[] for _ in editors]
        out = [[] for _ in columns]
        slots = [[] for _ in editors]
        # In split view, "new" widgets go right and "old" widgets left; unified has one column.
        sides = [None] if len(columns) == 1 else ["old", "new"]

        def reserve(widgets: list):
            rows = max(ed.rows_for(w) for ed, w in widgets)
            for ed_index, (ed, w) in enumerate(widgets):
                slots[editors.index(ed)].append((len(out[0]), rows, w))
            for col in out:
                col.extend([SLOT] * rows)

        if "top" in self.inline:
            target = editors[-1]
            reserve([(target, self.inline["top"])])
        for row in range(len(columns[0])):
            placed = []
            for c, col in enumerate(columns):
                out[c].append(col[row])
            for c, col in enumerate(columns):
                key = self._anchor(col[row], sides[c])
                if key and all(key != k for k, _ in placed):
                    placed.append((key, (editors[c], self.inline[key])))
            if placed:
                reserve([w for _k, w in placed])
        return out, slots

    def set_inline(self, widgets: dict, render: bool = True):
        """Widgets shown inside the diff, under their line; the previous ones are deleted."""
        keep = set(map(id, widgets.values()))
        for ed in (self.unified, self.left, self.right):
            ed.slots = []  # every editor: the hidden view must not keep the old widgets either
        for w in self.inline.values():
            if id(w) not in keep:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.inline = widgets
        if render and self.data is not None:
            scroll = self.current_scroll()
            self.render()
            QTimer.singleShot(0, lambda: [e.verticalScrollBar().setValue(scroll) for e in self.editors()])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.inline and abs(self.width() - self._inline_width) > 24:
            self._resize_timer.start()  # card heights depend on the width

    def _rewrap(self):
        if self.inline and self.data is not None:
            self.set_inline(dict(self.inline))

    def set_commentable(self, on: bool):
        """Merge request review: clickable line numbers, comment markers, no whole-file context."""
        for ed in (self.unified, self.left, self.right):
            ed.commentable = on
            ed._update_margins()
            ed._place_gutter()
        self.full.setVisible(not on)

    @staticmethod
    def line_key(line: Line) -> tuple[str, int] | None:
        if line.kind == "del":
            return "old", line.old
        if line.kind in ("add", "ctx"):
            return "new", line.new
        return None

    def set_markers(self, markers: dict[tuple[str, int], tuple[int, str]]):
        self.markers = markers
        self._apply_markers()

    def _apply_markers(self):
        for ed in (self.unified, self.left, self.right):
            found = {}
            for n, line in enumerate(ed.lines):
                key = self.line_key(line)
                mark = self.markers.get(key) if key else None
                if mark is None and line.kind == "ctx":
                    mark = self.markers.get(("old", line.old))
                if mark:
                    found[n] = mark
            ed.set_markers(found)

    def scroll_to(self, side: str, number: int):
        """Center the diff on a line (side "new" or "old") and select it."""
        for ed in self.editors():
            for n, line in enumerate(ed.lines):
                if (side == "new" and line.new == number and line.kind != "del") or (
                        side == "old" and line.old == number and line.kind != "add"):
                    c = QTextCursor(ed.document().findBlockByNumber(n))
                    ed.setTextCursor(c)
                    ed.centerCursor()
                    c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                    ed.setTextCursor(c)
                    return

    def editors(self) -> list[DiffEditor]:
        return {1: [self.unified], 2: [self.left, self.right]}.get(self.stack.currentIndex(), [])

    def set_find(self, term: str):
        self.find_edit.setText(term)

    def run_find(self):
        term = self.find_edit.text()
        self.matches = []
        for ed in (self.unified, self.left, self.right):
            ed.setExtraSelections([])
        for ed in self.editors():
            self.matches += [(ed, c) for c in ed.find_all(term)]
        self.matches.sort(key=lambda m: (m[1].blockNumber(), m[0] is self.right, m[1].position()))
        self.match_index = 0 if self.matches else -1
        self._show_find()

    def step_find(self, delta: int):
        if not self.matches:
            return
        self.match_index = (self.match_index + delta) % len(self.matches)
        self._show_find()

    def _show_find(self):
        term = self.find_edit.text()
        if not term:
            self.find_count.setText("")
            return
        self.find_count.setText(f"{self.match_index + 1}/{len(self.matches)}" if self.matches else "0")
        current = self.matches[self.match_index] if self.matches else None
        for ed in self.editors():
            ed.show_matches([c for e, c in self.matches if e is ed],
                            current[1] if current and current[0] is ed else None)
        if current:
            ed, cursor = current
            c = QTextCursor(cursor)
            c.clearSelection()
            ed.setTextCursor(c)
            ed.centerCursor()
