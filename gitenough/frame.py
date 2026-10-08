"""Frame of the secondary windows (Changes, History, merge requests...): drawn by GitEnough, inside the main window.

Windows gives a window owned by another no taskbar button: minimized, it shrinks to a tiny caption at the bottom
left of the screen, and maximized it covers the main window's own title bar. Here the window has its own title
bar, with buttons that look like GitEnough's and not like the main window's: maximize fills the main window's
area (its title bar stays reachable, so minimizing it hides everything at once), and minimize sends the window to
the dock at the bottom of the main window, labelled with its repository.
"""

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QWidget

from .style import C

EDGE = 5  # pixels of the window border that resize it
KINDS = {  # keep_size kind -> (badge, color)
    "changes": ("Changes", C["orange"]), "history": ("History", C["violet"]), "compare": ("Compare", C["blue"]),
    "branches": ("Branches", C["green"]), "stashes": ("Stashes", C["violet"]), "file_history": ("File", C["blue"]),
    "rebase": ("Rebase", C["pink"]), "cherry_pick": ("Cherry-pick", C["pink"]), "merge_tool": ("Merge", C["pink"]),
    "merge_requests": ("MRs", C["blue"]), "merge_request": ("MR", C["blue"]),
}


class TitleBar(QWidget):
    """Badge, title and the window's own buttons; drag to move, double-click to maximize in the app."""

    def __init__(self, frame: "Frame"):
        super().__init__()
        self.frame = frame
        badge, color = KINDS.get(frame.kind, (frame.kind.title(), C["accent"]))
        self.setObjectName("frameBar")
        self.setAttribute(Qt.WA_StyledBackground, True)  # a plain QWidget paints no stylesheet background
        self.setFixedHeight(34)
        self.setStyleSheet(f"""
            #frameBar {{ background: {C['surface2']}; border-bottom: 1px solid {C['border']};
                         border-left: 3px solid {color}; }}
            #frameBadge {{ color: {color}; font-weight: 700; font-size: 8.5pt; padding: 1px 7px;
                           border: 1px solid {color}; border-radius: 9px; }}
            #frameTitle {{ font-weight: 600; }}
            QToolButton#frameBtn {{ color: {C['muted']}; border: 1px solid {C['border']}; border-radius: 6px;
                                    padding: 0; min-width: 26px; max-width: 26px; min-height: 20px; max-height: 20px;
                                    font-size: 9pt; }}
            QToolButton#frameBtn:hover {{ color: {C['text']}; background: {C['hover']}; border-color: {color}; }}
            QToolButton#frameClose:hover {{ color: white; background: {C['red']}; border-color: {C['red']}; }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 8, 0)
        lay.setSpacing(8)
        tag = QLabel(badge)
        tag.setObjectName("frameBadge")
        self.title = QLabel(frame.win.windowTitle())
        self.title.setObjectName("frameTitle")
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(tag)
        lay.addWidget(self.title, 1)
        self.min_btn = self._button("–", "Minimize to the bar at the bottom of GitEnough", frame.minimize)
        self.max_btn = self._button("▢", "Fill the GitEnough window (double-click the title)", frame.toggle_max)
        self.close_btn = self._button("✕", "Close", frame.win.close)
        self.close_btn.setObjectName("frameClose")
        self.close_btn.setStyleSheet(f"QToolButton#frameClose {{ color: {C['muted']}; border: 1px solid {C['border']}; "
                                     "border-radius: 6px; min-width: 26px; max-width: 26px; min-height: 20px; "
                                     "max-height: 20px; font-size: 9pt; }")
        for b in (self.min_btn, self.max_btn, self.close_btn):
            lay.addWidget(b)

    def _button(self, text: str, tip: str, fn) -> QToolButton:
        b = QToolButton()
        b.setObjectName("frameBtn")
        b.setText(text)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.NoFocus)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        return b

    def set_maximized(self, on: bool):
        self.max_btn.setText("❐" if on else "▢")
        self.max_btn.setToolTip("Back to its own size (double-click the title)" if on
                                else "Fill the GitEnough window (double-click the title)")

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        if event.position().y() < EDGE and not self.frame.maximized:
            self.frame.start_resize(Qt.TopEdge)
            return
        self.frame.start_move(event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.frame.toggle_max()

    def mouseMoveEvent(self, event):
        top = event.position().y() < EDGE and not self.frame.maximized
        self.setCursor(Qt.SizeVerCursor if top else Qt.ArrowCursor)
        super().mouseMoveEvent(event)


class Frame(QObject):
    """Turns a top-level window owned by the main window into a framed one (see the module doc)."""

    changed = Signal()  # title or minimized state: the dock repaints

    def __init__(self, win: QWidget, main, kind: str):
        super().__init__(win)
        self.win, self.main, self.kind = win, main, kind
        self.maximized = False
        self.screen_max = None  # the other screen it fills, when maximized away from the main window
        self.minimized = False
        self.normal = QRect()  # geometry to go back to after an in-app maximize
        self.bar: TitleBar | None = None
        self.start_max = False
        win.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        win.setMouseTracking(True)
        win.installEventFilter(self)
        win.windowTitleChanged.connect(self._on_title)
        main.installEventFilter(self)
        dock = getattr(main, "dock", None)
        if dock is not None:
            dock.add(self)

    # ---------- state ----------
    @property
    def title(self) -> str:
        return self.win.windowTitle()

    @property
    def color(self) -> str:
        return KINDS.get(self.kind, ("", C["accent"]))[1]

    def _on_title(self, text: str):
        if self.bar is not None:
            self.bar.title.setText(text)
        self.changed.emit()

    def area(self) -> QRect:
        """Where a maximized window goes: the main window's inside, above the dock, or the whole screen when the
        window was maximized on another screen than the main window's."""
        if self.screen_max is not None:
            return self.screen_max.availableGeometry()
        rect = self.main.geometry()  # client area, without the main window's title bar
        dock = getattr(self.main, "dock", None)
        if dock is not None and dock.isVisible():
            rect.setBottom(dock.mapToGlobal(QPoint(0, 0)).y() - 2)
        return rect

    def maximize(self):
        if not self.maximized:
            self.normal = self.win.geometry()
            screen = QGuiApplication.screenAt(self.win.geometry().center()) or self.win.screen()
            self.screen_max = screen if screen is not None and screen != self.main.screen() else None
        self.maximized = True
        self.win.setGeometry(self.area())
        if self.bar is not None:
            self.bar.set_maximized(True)

    def restore(self):
        if not self.maximized:
            return
        self.maximized = False
        if self.normal.isValid():
            self.win.setGeometry(self.normal)
        if self.bar is not None:
            self.bar.set_maximized(False)

    def toggle_max(self):
        self.restore() if self.maximized else self.maximize()

    def minimize(self):
        if getattr(self.main, "dock", None) is None:
            self.win.showMinimized()
            return
        self.minimized = True
        self.win.hide()
        self.changed.emit()

    def bring_back(self):
        """From the dock: show the window again, in front."""
        self.minimized = False
        self.win.show()
        if self.maximized:
            self.win.setGeometry(self.area())
        self.win.raise_()
        self.win.activateWindow()
        self.changed.emit()

    # ---------- move and resize through the system (snapping, multi-screen) ----------
    def start_move(self, global_pos: QPoint):
        if self.maximized:
            # Leave the maximized size under the pointer, keeping the same relative spot of the title bar.
            geo = self.win.geometry()
            ratio = (global_pos.x() - geo.x()) / max(geo.width(), 1)
            self.restore()
            size = self.win.geometry()
            self.win.move(global_pos.x() - int(size.width() * ratio), global_pos.y() - 17)
        handle = self.win.windowHandle()
        if handle is not None:
            handle.startSystemMove()

    def start_resize(self, edges):
        handle = self.win.windowHandle()
        if handle is not None and not self.maximized:
            handle.startSystemResize(edges)

    def _edges(self, pos: QPoint):
        w, h = self.win.width(), self.win.height()
        edges = Qt.Edges()
        if pos.x() < EDGE:
            edges |= Qt.LeftEdge
        if pos.x() >= w - EDGE:
            edges |= Qt.RightEdge
        if pos.y() >= h - EDGE:
            edges |= Qt.BottomEdge
        return edges

    def eventFilter(self, obj, event):
        kind = event.type()
        if obj is self.main:
            if self.maximized and self.screen_max is None and self.win.isVisible():
                # Right away, in the event: deferring it made the window trail behind a fast drag.
                if kind == QEvent.Move:
                    area = self.area()
                    if area.size() == self.win.size():
                        self.win.move(area.topLeft())  # a plain move: no relayout of the window's content
                    else:
                        self.win.setGeometry(area)
                elif kind == QEvent.Resize:
                    self.win.setGeometry(self.area())
            return False
        if kind == QEvent.Show:
            if self.bar is None:
                self._install_bar()
            if self.minimized:  # shown again from elsewhere (its button in the main list)
                self.minimized = False
                self.changed.emit()
        elif kind == QEvent.WindowStateChange and self.win.isMaximized():
            # Aero snap to the top of the screen or Win+Up: maximize inside the app instead.
            QTimer.singleShot(0, self._unsnap)
        elif kind == QEvent.MouseMove and not self.maximized:
            edges = self._edges(event.position().toPoint())
            cursors = {Qt.LeftEdge: Qt.SizeHorCursor, Qt.RightEdge: Qt.SizeHorCursor, Qt.BottomEdge: Qt.SizeVerCursor,
                       Qt.LeftEdge | Qt.BottomEdge: Qt.SizeBDiagCursor, Qt.RightEdge | Qt.BottomEdge: Qt.SizeFDiagCursor}
            self.win.setCursor(cursors.get(edges, Qt.ArrowCursor))
        elif kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            edges = self._edges(event.position().toPoint())
            if edges:
                self.start_resize(edges)
                return True
        return False

    def _unsnap(self):
        if self.win.isMaximized():
            self.win.showNormal()
            self.maximize()

    def _install_bar(self):
        layout = self.win.layout()
        if not isinstance(layout, QBoxLayout):
            return
        self.bar = TitleBar(self)
        layout.insertWidget(0, self.bar)
        m = layout.contentsMargins()
        # A thin border the pointer can grab to resize; the title bar covers the top.
        layout.setContentsMargins(max(m.left(), EDGE - 1), 0, max(m.right(), EDGE - 1), max(m.bottom(), EDGE - 1))
        if self.start_max:
            QTimer.singleShot(0, self.maximize)


def frame_of(win) -> "Frame | None":
    return win.findChild(Frame, options=Qt.FindDirectChildrenOnly)


def adopt(win, kind: str, maximized: bool) -> "Frame | None":
    """Called by keep_size, before the window is shown: frame it when the main window owns it."""
    parent = win.parentWidget()
    main = parent.window() if parent is not None else None
    if main is None or not hasattr(main, "dock"):
        return None
    frame = Frame(win, main, kind)
    frame.start_max = maximized
    return frame


class _Chip(QPushButton):
    def __init__(self, frame: Frame, dock: "WindowDock"):
        super().__init__()
        self.frame, self.dock = frame, dock
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.clicked.connect(self._click)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self.refresh()

    def refresh(self):
        f = self.frame
        self.setText(("▸  " if f.minimized else "") + f.title)
        self.setToolTip("Minimized: click to bring it back (middle click closes it)" if f.minimized
                        else "Click to minimize it (middle click closes it)")
        self.setStyleSheet(f"""
            QPushButton {{ text-align: left; padding: 7px 16px 7px 12px; border-radius: 8px; font-size: 10pt;
                           border: 1px solid {f.color if f.minimized else C['border']};
                           border-left: 4px solid {f.color};
                           background: {C['surface2'] if f.minimized else C['surface']};
                           color: {C['text'] if f.minimized else C['muted']}; font-weight: 600; }}
            QPushButton:hover {{ background: {C['hover']}; color: {C['text']}; }}""")

    def _click(self):
        f = self.frame
        # Clicking the chip activates the main window first: on screen means "minimize it", like a taskbar.
        if f.minimized or not f.win.isVisible():
            f.bring_back()
        else:
            f.minimize()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self.frame.win.close()
            return
        super().mouseReleaseEvent(event)


class WindowDock(QWidget):
    """Bar at the bottom of the main window: one chip per open window, minimized ones highlighted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.chips: dict[Frame, _Chip] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(6)
        label = QLabel("Windows")
        label.setObjectName("muted")
        lay.addWidget(label)
        self.row = QHBoxLayout()
        self.row.setSpacing(6)
        lay.addLayout(self.row)
        lay.addStretch()
        self.hide()

    def add(self, frame: Frame):
        chip = _Chip(frame, self)
        self.chips[frame] = chip
        self.row.addWidget(chip)
        frame.changed.connect(chip.refresh)
        frame.win.destroyed.connect(lambda *_: self._remove(frame))
        self.setVisible(True)

    def _remove(self, frame: Frame):
        chip = self.chips.pop(frame, None)
        try:
            if chip is not None:
                chip.deleteLater()
            self.setVisible(bool(self.chips))
        except RuntimeError:
            pass  # the app is closing: the dock went first

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(QColor(C["border"]))
        p.drawLine(0, 0, self.width(), 0)
