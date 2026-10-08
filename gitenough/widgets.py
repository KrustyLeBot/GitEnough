from functools import lru_cache

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractScrollArea, QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget,
                               QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate,
                               QToolButton, QVBoxLayout, QWidget)

from .errors import Explained
from .repo import FileChange
from .style import C

CODE_COLORS = {"M": C["orange"], "A": C["green"], "D": C["red"], "R": C["blue"], "C": C["blue"],
               "T": C["violet"], "U": C["pink"], "?": C["green"]}
CODE_NAMES = {"M": "Modified", "A": "Added", "D": "Deleted", "R": "Renamed", "C": "Copied",
              "T": "Type changed", "U": "Conflict", "?": "Untracked"}


def _draw(name: str, p: QPainter, s: float):
    if name == "tree":
        path = QPainterPath(QPointF(5 * s, 4 * s))
        path.lineTo(5 * s, 16 * s)
        path.moveTo(5 * s, 13 * s)
        path.cubicTo(5 * s, 9 * s, 15 * s, 11 * s, 15 * s, 7 * s)
        p.drawPath(path)
        for x, y in ((5, 4), (5, 16), (15, 6)):
            p.drawEllipse(QPointF(x * s, y * s), 2.2 * s, 2.2 * s)
    elif name == "folder":
        path = QPainterPath(QPointF(2.5 * s, 5 * s))
        path.lineTo(8 * s, 5 * s)
        path.lineTo(10 * s, 7 * s)
        path.lineTo(17.5 * s, 7 * s)
        path.lineTo(17.5 * s, 15.5 * s)
        path.lineTo(2.5 * s, 15.5 * s)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "changes":
        p.drawRoundedRect(QRectF(4 * s, 2.5 * s, 12 * s, 15 * s), 2 * s, 2 * s)
        p.drawLine(QPointF(10 * s, 6 * s), QPointF(10 * s, 11 * s))
        p.drawLine(QPointF(7.5 * s, 8.5 * s), QPointF(12.5 * s, 8.5 * s))
        p.drawLine(QPointF(7.5 * s, 14 * s), QPointF(12.5 * s, 14 * s))
    elif name == "refresh":
        path = QPainterPath()
        path.arcMoveTo(QRectF(4 * s, 4 * s, 12 * s, 12 * s), 60)
        path.arcTo(QRectF(4 * s, 4 * s, 12 * s, 12 * s), 60, 280)
        p.drawPath(path)
        p.drawLine(QPointF(13 * s, 3 * s), QPointF(13.2 * s, 5.4 * s))
        p.drawLine(QPointF(13.2 * s, 5.4 * s), QPointF(10.6 * s, 5.8 * s))
    elif name == "plus":
        p.drawLine(QPointF(10 * s, 4.5 * s), QPointF(10 * s, 15.5 * s))
        p.drawLine(QPointF(4.5 * s, 10 * s), QPointF(15.5 * s, 10 * s))
    elif name == "minus":
        p.drawLine(QPointF(4.5 * s, 10 * s), QPointF(15.5 * s, 10 * s))
    elif name == "discard":
        path = QPainterPath()
        path.arcMoveTo(QRectF(4 * s, 5 * s, 12 * s, 11 * s), 160)
        path.arcTo(QRectF(4 * s, 5 * s, 12 * s, 11 * s), 160, -250)
        p.drawPath(path)
        p.drawLine(QPointF(4.2 * s, 8.8 * s), QPointF(3.6 * s, 5.2 * s))
        p.drawLine(QPointF(4.2 * s, 8.8 * s), QPointF(7.6 * s, 8.2 * s))
    elif name == "up":
        p.drawLine(QPointF(10 * s, 4 * s), QPointF(10 * s, 16 * s))
        p.drawLine(QPointF(5 * s, 9 * s), QPointF(10 * s, 4 * s))
        p.drawLine(QPointF(15 * s, 9 * s), QPointF(10 * s, 4 * s))
    elif name == "globe":
        p.drawEllipse(QPointF(10 * s, 10 * s), 7 * s, 7 * s)
        p.drawEllipse(QPointF(10 * s, 10 * s), 3 * s, 7 * s)
        p.drawLine(QPointF(3 * s, 10 * s), QPointF(17 * s, 10 * s))
    elif name == "code":
        p.drawPolyline([QPointF(7 * s, 6 * s), QPointF(3 * s, 10 * s), QPointF(7 * s, 14 * s)])
        p.drawPolyline([QPointF(13 * s, 6 * s), QPointF(17 * s, 10 * s), QPointF(13 * s, 14 * s)])
        p.drawLine(QPointF(11.2 * s, 5 * s), QPointF(8.8 * s, 15 * s))
    elif name == "vs":
        font = QFont()
        font.setPixelSize(int(9.5 * s))
        font.setBold(True)
        p.setFont(font)
        p.drawRoundedRect(QRectF(2.5 * s, 3.5 * s, 15 * s, 13 * s), 2.5 * s, 2.5 * s)
        p.drawText(QRectF(2.5 * s, 3.5 * s, 15 * s, 13 * s), Qt.AlignCenter, "VS")
    elif name == "branches":
        for y in (5, 10, 15):
            p.drawEllipse(QPointF(4.5 * s, y * s), 1.6 * s, 1.6 * s)
            p.drawLine(QPointF(8 * s, y * s), QPointF(16.5 * s, y * s))
    elif name == "stash":
        p.drawRoundedRect(QRectF(3 * s, 7 * s, 14 * s, 9.5 * s), 1.5 * s, 1.5 * s)
        p.drawRoundedRect(QRectF(2 * s, 3.5 * s, 16 * s, 3.5 * s), 1.2 * s, 1.2 * s)
        p.drawLine(QPointF(8 * s, 10 * s), QPointF(12 * s, 10 * s))
    elif name == "gear":
        import math
        p.drawEllipse(QPointF(10 * s, 10 * s), 2.6 * s, 2.6 * s)
        path = QPainterPath()
        for i in range(16):
            angle = math.pi * 2 * i / 16
            radius = (7.2 if i % 2 == 0 else 5.4) * s
            point = QPointF(10 * s + radius * math.cos(angle), 10 * s + radius * math.sin(angle))
            path.moveTo(point) if i == 0 else path.lineTo(point)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "pin":
        path = QPainterPath(QPointF(10 * s, 17 * s))
        path.lineTo(10 * s, 12 * s)
        p.drawPath(path)
        p.drawLine(QPointF(5 * s, 12 * s), QPointF(15 * s, 12 * s))
        p.drawPolyline([QPointF(7 * s, 12 * s), QPointF(7.5 * s, 4 * s), QPointF(12.5 * s, 4 * s),
                        QPointF(13 * s, 12 * s)])
    elif name == "tree_view":
        p.drawLine(QPointF(4 * s, 4 * s), QPointF(4 * s, 16 * s))
        for y, x in ((5, 7), (10, 9), (15, 9)):
            p.drawLine(QPointF(4 * s, y * s), QPointF(x * s, y * s))
            p.drawLine(QPointF((x + 2) * s, y * s), QPointF(17 * s, y * s))
    elif name == "compare":
        p.drawRoundedRect(QRectF(2.5 * s, 4 * s, 6.5 * s, 12 * s), 1.5 * s, 1.5 * s)
        p.drawRoundedRect(QRectF(11 * s, 4 * s, 6.5 * s, 12 * s), 1.5 * s, 1.5 * s)
        p.drawLine(QPointF(9 * s, 10 * s), QPointF(11 * s, 10 * s))
    elif name == "chev_up":
        p.drawPolyline([QPointF(5 * s, 12.5 * s), QPointF(10 * s, 7.5 * s), QPointF(15 * s, 12.5 * s)])
    elif name == "chev_down":
        p.drawPolyline([QPointF(5 * s, 7.5 * s), QPointF(10 * s, 12.5 * s), QPointF(15 * s, 7.5 * s)])
    elif name == "terminal":
        p.drawRoundedRect(QRectF(2.5 * s, 4 * s, 15 * s, 12 * s), 2 * s, 2 * s)
        p.drawPolyline([QPointF(6 * s, 8 * s), QPointF(8.5 * s, 10 * s), QPointF(6 * s, 12 * s)])
        p.drawLine(QPointF(10 * s, 12.5 * s), QPointF(14 * s, 12.5 * s))
    elif name == "powershell":
        # Slanted window, like the PowerShell logo.
        p.drawPolygon([QPointF(5 * s, 4 * s), QPointF(18 * s, 4 * s), QPointF(15 * s, 16 * s), QPointF(2 * s, 16 * s)])
        p.drawPolyline([QPointF(6.5 * s, 7.5 * s), QPointF(9.5 * s, 10 * s), QPointF(5.5 * s, 12.5 * s)])
        p.drawLine(QPointF(10 * s, 12.5 * s), QPointF(13 * s, 12.5 * s))
    elif name == "open":
        # A window with an arrow leaving it.
        path = QPainterPath(QPointF(9 * s, 4 * s))
        path.lineTo(4 * s, 4 * s)
        path.lineTo(4 * s, 16 * s)
        path.lineTo(16 * s, 16 * s)
        path.lineTo(16 * s, 11 * s)
        p.drawPath(path)
        p.drawLine(QPointF(9.5 * s, 10.5 * s), QPointF(16.5 * s, 3.5 * s))
        p.drawPolyline([QPointF(11.5 * s, 3.5 * s), QPointF(16.5 * s, 3.5 * s), QPointF(16.5 * s, 8.5 * s)])


@lru_cache(maxsize=None)
def icon(name: str, size: int = 18) -> QIcon:
    result = QIcon()
    for mode, color in ((QIcon.Normal, C["muted"]), (QIcon.Active, C["text"]), (QIcon.Disabled, "#3a4150")):
        for scale in (1, 2):
            px = size * scale
            pm = QPixmap(px, px)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(color), 1.6 * px / 20, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            _draw(name, p, px / 20)
            p.end()
            pm.setDevicePixelRatio(scale)
            result.addPixmap(pm, mode)
    return result


def set_css(widget, css: str) -> None:
    """setStyleSheet only when it changes: re-polishing a widget is expensive and rows re-render often."""
    if widget.property("_css") != css:
        widget.setProperty("_css", css)
        widget.setStyleSheet(css)


def icon_button(name: str, tooltip: str, size: int = 18) -> QToolButton:
    btn = QToolButton()
    btn.setIcon(icon(name, size))
    btn.setIconSize(QSize(size, size))
    btn.setToolTip(tooltip)
    btn.setAutoRaise(True)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


class ElidedLabel(QLabel):
    """Single-line label that elides instead of growing: long file names must never resize a window."""

    def __init__(self, text: str = "", mode=Qt.ElideMiddle, parent=None):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(20)
        self.setText(text)

    def setText(self, text: str):
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._elide()  # Stylesheet fonts (bold titles) change the text width.

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(self._full, self._mode, max(self.width(), 20)))

    def sizeHint(self) -> QSize:
        return QSize(min(self.fontMetrics().horizontalAdvance(self._full) + 4, 600), super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(20, super().minimumSizeHint().height())


class NoWheelComboBox(QComboBox):
    """Combo that never reacts to the mouse wheel.

    In a scrolling list, a wheel notch over a plain QComboBox changes its value (here: runs git switch)
    instead of scrolling the list. The wheel is handed to the enclosing list's scroll bar instead.
    """

    def wheelEvent(self, event):
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QAbstractScrollArea):
            parent = parent.parentWidget()
        if parent is not None:
            QApplication.sendEvent(parent.verticalScrollBar(), event)
        else:
            event.ignore()


class FileDelegate(QStyledItemDelegate):
    """List row: colored status badge, file name, then its directory in muted text."""

    ROW_HEIGHT = 30

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, p: QPainter, option, index):
        fc: FileChange = index.data(Qt.UserRole)
        if fc is None:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(4, 1, -4, -1)
        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#262c3a"))
            p.drawRoundedRect(r, 6, 6)
        elif option.state & QStyle.State_MouseOver:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["hover"]))
            p.drawRoundedRect(r, 6, 6)

        color = QColor(CODE_COLORS.get(fc.code, C["muted"]))
        badge = QRectF(r.left() + 8, r.center().y() - 8, 18, 16)
        tint = QColor(color)
        tint.setAlpha(40)
        p.setPen(Qt.NoPen)
        p.setBrush(tint)
        p.drawRoundedRect(badge, 4, 4)
        font = QFont(option.font)
        font.setBold(True)
        font.setPointSizeF(8)
        p.setFont(font)
        p.setPen(color)
        p.drawText(badge, Qt.AlignCenter, "U" if fc.code == "?" else fc.code)

        name = fc.path.rsplit("/", 1)[-1]
        folder = fc.path[: -len(name)].rstrip("/")
        if fc.orig:
            folder = f"{fc.orig}  →  {folder or '.'}"
        x = badge.right() + 10
        font = QFont(option.font)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        p.setPen(QColor(C["text"]))
        text_rect = QRectF(x, r.top(), r.right() - x - 8, r.height())
        fm = p.fontMetrics()
        shown = fm.elidedText(name, Qt.ElideMiddle, int(text_rect.width()))
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, shown)
        used = fm.horizontalAdvance(shown) + 8
        if folder and used < text_rect.width() - 20:
            p.setFont(option.font)
            p.setPen(QColor(C["faint"]))
            rest = QRectF(x + used, r.top(), text_rect.width() - used, r.height())
            p.drawText(rest, Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(folder, Qt.ElideLeft, int(rest.width())))
        p.restore()


def file_list() -> QListWidget:
    lw = QListWidget()
    lw.setItemDelegate(FileDelegate(lw))
    lw.setMouseTracking(True)
    lw.setUniformItemSizes(True)
    lw.setObjectName("files")
    lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return lw


def confirm_discard_all(parent, name: str, count: int, kept: int = 0) -> str | None:
    """Returns "discard", "stash" or None. kept: never-commit changes, which both choices leave in place."""
    one = kept == 1
    box = QMessageBox(QMessageBox.Warning, "Discard all changes",
                      f"Discard every local change in {name}?\n\n"
                      f"{count} file(s): staged, unstaged and untracked changes are all lost. "
                      "Ignored files are kept. This cannot be undone.\n\n"
                      "Stash instead puts them aside; restore them later with git stash pop."
                      + (f"\n\n🔒 {kept} never-commit change{'' if one else 's'} stay{'s' if one else ''} in the "
                         "files: neither discarded nor stashed. To discard them too, allow committing them first "
                         "(Changes window, Manage…)." if kept else ""),
                      QMessageBox.Cancel, parent)
    stash = box.addButton("Stash instead", QMessageBox.AcceptRole)
    discard = box.addButton("Discard everything", QMessageBox.DestructiveRole)
    discard.setObjectName("danger")
    box.setDefaultButton(QMessageBox.Cancel)
    box.exec()
    clicked = box.clickedButton()
    return "stash" if clicked is stash else "discard" if clicked is discard else None


class ErrorDialog(QDialog):
    """Readable explanation of a git failure, with an optional "Stash & retry" action."""

    def __init__(self, ex: Explained, subject: str, parent=None, retry_label: str = ""):
        super().__init__(parent)
        self.setWindowTitle(ex.title)
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(14)
        badge = QLabel("!")
        badge.setFixedSize(38, 38)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"background: rgba(242,107,107,0.16); color: {C['red']}; border-radius: 19px;"
                            "font-size: 16pt; font-weight: 800;")
        head.addWidget(badge, 0, Qt.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(ex.title)
        title.setStyleSheet("font-size: 13pt; font-weight: 700;")
        sub = QLabel(subject)
        sub.setObjectName("muted")
        titles.addWidget(title)
        titles.addWidget(sub)
        head.addLayout(titles, 1)
        lay.addLayout(head)

        detail = QLabel(ex.detail)
        detail.setWordWrap(True)
        lay.addWidget(detail)
        if ex.files:
            cap = QLabel(f"{len(ex.files)} file{'s' if len(ex.files) > 1 else ''} involved")
            cap.setObjectName("muted")
            lay.addWidget(cap)
            files = QPlainTextEdit("\n".join(ex.files))
            files.setReadOnly(True)
            files.setMaximumHeight(min(28 + 18 * len(ex.files), 180))
            lay.addWidget(files)
        if ex.hint:
            hint = QLabel(ex.hint)
            hint.setWordWrap(True)
            hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
            hint.setStyleSheet(f"background: {C['surface2']}; border: 1px solid {C['border']};"
                               f"border-radius: 8px; padding: 10px 12px; color: {C['muted']};")
            lay.addWidget(hint)

        self.raw = QPlainTextEdit(ex.raw)
        self.raw.setReadOnly(True)
        self.raw.setMaximumHeight(160)
        self.raw.hide()

        buttons = QHBoxLayout()
        toggle = QPushButton("Show git output")
        toggle.setObjectName("ghost")
        toggle.setVisible(bool(ex.raw))
        toggle.clicked.connect(lambda: (self.raw.setVisible(not self.raw.isVisible()),
                                        toggle.setText("Hide git output" if self.raw.isVisible()
                                                       else "Show git output"), self.adjustSize()))
        buttons.addWidget(toggle)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        if retry_label and ex.can_stash:
            retry = QPushButton(retry_label)
            retry.setObjectName("primary")
            retry.clicked.connect(self.accept)
            buttons.addWidget(retry)
            retry.setDefault(True)
        lay.addWidget(self.raw)
        lay.addLayout(buttons)


class _SizeKeeper(QObject):
    """Saves a window's size under its kind whenever the user resizes it."""

    def __init__(self, win, config, kind: str, persist: bool):
        super().__init__(win)
        self.win, self.config, self.kind, self.persist = win, config, kind, persist
        self.timer = QTimer(self, singleShot=True, interval=600)  # one save per drag, not per pixel
        self.timer.timeout.connect(self.save)
        self.max_before_minimize = None  # set while the owner window is minimized
        win.installEventFilter(self)
        parent = win.parentWidget()
        self.owner = parent.window() if parent is not None else None
        if self.owner is not None:
            self.owner.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.owner and event.type() == QEvent.WindowStateChange and self._frame() is None:
            # Qt on Windows un-maximizes owned windows when their owner comes back from the taskbar.
            if obj.isMinimized():
                self.max_before_minimize = self.win.isMaximized()
            elif self.max_before_minimize is not None:
                was_max, self.max_before_minimize = self.max_before_minimize, None
                if was_max:
                    QTimer.singleShot(50, self._remaximize)
        elif obj is self.win:
            kind = event.type()
            if kind in (QEvent.Resize, QEvent.WindowStateChange) and obj.isVisible():
                self.timer.start()
            elif kind in (QEvent.Close, QEvent.Hide) and self.timer.isActive():
                self.timer.stop()
                self.save()
        return False

    def _frame(self):
        from .frame import frame_of
        return frame_of(self.win)

    def _remaximize(self):
        if self.win.isVisible() and not self.win.isMaximized():
            self.win.showMaximized()

    def save(self):
        win = self.win
        if win.isMinimized() or win.isFullScreen():
            return
        frame = self._frame()
        maximized = frame.maximized if frame is not None else win.isMaximized()
        # Maximized: keep the size it goes back to, not the screen size.
        if frame is not None:
            size = frame.normal.size() if maximized else win.size()
        else:
            size = win.normalGeometry().size() if maximized else win.size()
        old = self.config.window_sizes.get(self.kind)
        if maximized and (not size.isValid() or size.isEmpty()):
            size = QSize(old[0], old[1]) if old else win.size()
        entry = [size.width(), size.height(), maximized]
        if entry != old:
            self.config.window_sizes[self.kind] = entry
            if self.persist:
                self.config.save()


def keep_size(win, config, kind: str, persist: bool = True) -> None:
    """Open win at the size the last window of this kind had, and remember its future resizes.

    Call after the default resize() and before show(). persist=False only records the size in memory,
    for windows editing a config whose unsaved changes must not reach the disk.
    """
    from .frame import adopt  # imported here: frame.py imports this module

    entry = config.window_sizes.get(kind)
    maximized = bool(entry and len(entry) > 2 and entry[2])
    framed = None
    if win.isWindow() and not isinstance(win, QDialog):
        framed = adopt(win, kind, maximized)  # maximizes inside the main window once shown
    if entry and len(entry) >= 2:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        w, h = int(entry[0]), int(entry[1])
        if area is not None:  # A smaller screen than last time: never open larger than it.
            w, h = min(w, area.width()), min(h, area.height())
        win.resize(max(w, 300), max(h, 200))
        if maximized and framed is None:
            win.setWindowState(win.windowState() | Qt.WindowMaximized)
    _SizeKeeper(win, config, kind, persist)



def ai_error_dialog(parent, text: str, title: str = "Claude") -> None:
    """A Claude Code failure, with a sign-in button when that is what is missing."""
    from . import ai

    box = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.Close, parent)
    sign_in = None
    if "not signed in" in text or "auth login" in text:
        sign_in = box.addButton("Sign in to Claude", QMessageBox.AcceptRole)
    elif "not installed" in text:
        box.setTextFormat(Qt.MarkdownText)
    box.exec()
    if sign_in is not None and box.clickedButton() is sign_in:
        ai.open_login()


class Spinner(QWidget):
    """Small turning arc shown while something loads; it only animates while visible."""

    def __init__(self, size: int = 16, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.angle = 0
        self.timer = QTimer(self, interval=50)
        self.timer.timeout.connect(self._step)

    def _step(self):
        self.angle = (self.angle + 30) % 360
        self.update()

    def showEvent(self, event):
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(C["accent"]), 2.2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        r = QRectF(2, 2, self.width() - 4, self.height() - 4)
        p.drawArc(r, -self.angle * 16, 270 * 16)
