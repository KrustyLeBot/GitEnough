"""History of one file (commits and their diff) and its blame."""

import time

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QSplitter, QTabWidget,
                               QVBoxLayout, QWidget)

from . import repo
from .diff_view import DiffEditor, DiffView, Line, lexer_for, parse_diff
from .repo import BlameLine, Commit
from .style import C
from .tree_window import rel_time
from .widgets import ElidedLabel, icon_button, keep_size

AGE_COLORS = ["#7c8cff", "#6d7fe6", "#5f71cc", "#5263b3", "#465599", "#3b4880", "#313b66", "#282f4d"]


class BlameEditor(DiffEditor):
    """File content with a gutter naming the commit, author and age of every run of lines."""

    GUTTER = 350

    def __init__(self):
        super().__init__("new")
        self.blame: list[BlameLine] = []
        self._rank: dict[str, int] = {}

    def gutter_width(self) -> int:
        return self.GUTTER

    def set_blame(self, lines: list[BlameLine], filename: str):
        self.blame = lines
        # Age buckets: newest commits get the brightest bar.
        order = sorted({b.sha for b in lines}, key=lambda sha: -next(b.time for b in lines if b.sha == sha))
        self._rank = {sha: min(i * len(AGE_COLORS) // max(len(order), 1), len(AGE_COLORS) - 1)
                      for i, sha in enumerate(order)}
        self.set_lines([Line("ctx", i + 1, i + 1, b.text) for i, b in enumerate(lines)],
                       lexer_for(filename.rsplit("/", 1)[-1]) if len(lines) <= 8000 else None)

    def paint_gutter(self, event):
        p = QPainter(self.gutter)
        p.fillRect(event.rect(), QColor("#12141a"))
        fm = self.fontMetrics()
        width = self.gutter.width()
        block = self.firstVisibleBlock()
        n = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        num_w = fm.horizontalAdvance("99999")
        while block.isValid() and top <= event.rect().bottom():
            if bottom >= event.rect().top() and n < len(self.blame):
                b = self.blame[n]
                h = int(bottom - top)
                p.fillRect(QRect(0, int(top), 3, h), QColor(AGE_COLORS[self._rank.get(b.sha, 0)]))
                if b.first:
                    p.fillRect(QRect(3, int(top), width - 3, 1), QColor("#1e222b"))
                    p.setPen(QColor(C["accent"] if not b.sha.startswith("0000") else C["yellow"]))
                    sha = "local" if b.sha.startswith("0000") else b.sha[:7]
                    p.drawText(QRect(10, int(top), 64, h), Qt.AlignVCenter | Qt.AlignLeft, sha)
                    p.setPen(QColor(C["muted"]))
                    info = f"{b.author}  ·  {rel_time(b.time)}" if b.time else b.author
                    area = QRect(76, int(top), width - 76 - num_w - 14, h)
                    p.drawText(area, Qt.AlignVCenter | Qt.AlignLeft, fm.elidedText(info, Qt.ElideRight, area.width()))
                p.setPen(QColor(C["faint"]))
                p.drawText(QRect(width - num_w - 8, int(top), num_w, h), Qt.AlignVCenter | Qt.AlignRight, str(n + 1))
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            n += 1

    def line_at(self, pos) -> BlameLine | None:
        n = self.cursorForPosition(pos).block().blockNumber()
        return self.blame[n] if 0 <= n < len(self.blame) else None

    def mouseMoveEvent(self, event):
        b = self.line_at(event.position().toPoint())
        self.viewport().setToolTip(f"{b.sha[:10]}  {b.author}\n{b.summary}" if b else "")
        super().mouseMoveEvent(event)


class FileHistoryWindow(QWidget):
    def __init__(self, main, project, file: str):
        super().__init__(main, Qt.Window)
        self.main, self.p, self.tasks = main, project, main.tasks
        self.path, self.file = project.path, file
        self.entries: list[tuple[Commit, str]] = []
        self.commit: tuple[Commit, str] | None = None

        self.setWindowTitle(f"{file} · {project.name}")
        self.resize(1400, 860)
        keep_size(self, self.main.config, "file_history")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 14, 16, 14)
        title = ElidedLabel(file, Qt.ElideLeft)
        title.setStyleSheet("font-size: 13pt; font-weight: 700;")
        hl.addWidget(title, 1)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        hl.addWidget(self.status)
        refresh = icon_button("refresh", "Refresh (F5)")
        refresh.clicked.connect(self.refresh)
        hl.addWidget(refresh)
        root.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        # History tab: commits touching the file, and the file's diff in the selected one.
        hist = QSplitter(Qt.Horizontal)
        hist.setHandleWidth(1)
        self.list = QListWidget()
        self.list.setObjectName("files")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self.on_commit)
        wrap = QWidget()
        wrap.setObjectName("sidePanel")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(12, 12, 12, 12)
        wl.addWidget(self.list)
        self.diff = DiffView()
        self.diff.options_changed.connect(lambda: self.commit and self.show_diff())
        hist.addWidget(wrap)
        hist.addWidget(self.diff)
        hist.setSizes([420, 980])
        self.tabs.addTab(hist, "History")
        # Blame tab.
        self.blame = BlameEditor()
        self.blame.mouseDoubleClickEvent = self._blame_double_click
        blame_wrap = QWidget()
        blame_wrap.setObjectName("sidePanel")
        bl = QVBoxLayout(blame_wrap)
        bl.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("  Hover a line for its commit; double-click to open that commit in History.")
        hint.setObjectName("muted")
        bl.addWidget(hint)
        bl.addWidget(self.blame, 1)
        self.tabs.addTab(blame_wrap, "Blame")
        self.tabs.currentChanged.connect(lambda i: i == 1 and not self.blame.blame and self.load_blame())
        root.addWidget(self.tabs, 1)
        QShortcut(QKeySequence("F5"), self, self.refresh)
        self.refresh()

    def refresh(self):
        self.status.setText("Loading…")
        self.tasks.submit(repo.file_history, self._on_history, self.path, self.file)
        if self.tabs.currentIndex() == 1:
            self.load_blame()

    def _on_history(self, entries, error):
        if error:
            self.status.setText(str(error))
            return
        self.entries = entries
        self.status.setText(f"{len(entries)} commit{'s' if len(entries) != 1 else ''}")
        self.list.blockSignals(True)
        self.list.clear()
        for c, name in entries:
            renamed = f"   ·   as {name}" if name != self.file else ""
            item = QListWidgetItem(f"{c.subject}\n{c.sha[:8]}  ·  {c.author}  ·  {rel_time(c.time)}{renamed}")
            item.setToolTip(time.strftime("%Y-%m-%d %H:%M", time.localtime(c.time)))
            self.list.addItem(item)
        self.list.blockSignals(False)
        if entries:
            self.list.setCurrentRow(0)

    def on_commit(self, row: int):
        if 0 <= row < len(self.entries):
            self.commit = self.entries[row]
            self.show_diff()

    def show_diff(self):
        (c, name), ws, full = self.commit, self.diff.ignore_ws, self.diff.full_file
        self.diff.set_message("Loading…", name)
        entry = self.commit
        self.tasks.submit(lambda: parse_diff(repo.file_commit_diff(self.path, c, name, ws, full)),
                          lambda data, err: self._on_diff(entry, data, err))

    def _on_diff(self, entry, data, error):
        if self.commit is not entry:
            return
        if error:
            self.diff.set_message(str(error))
        else:
            self.diff.set_diff(entry[1], data)

    def load_blame(self):
        self.status.setText("Blaming…")
        self.tasks.submit(repo.blame, self._on_blame, self.path, self.file)

    def _on_blame(self, lines, error):
        if error:
            self.status.setText(str(error))
            return
        self.blame.set_blame(lines, self.file)
        authors = len({b.author for b in lines})
        self.status.setText(f"{len(lines)} lines · {len({b.sha for b in lines})} commits · {authors} author"
                            f"{'s' if authors != 1 else ''}")

    def _blame_double_click(self, event):
        b = self.blame.line_at(event.position().toPoint())
        if not b:
            return
        row = next((i for i, (c, _n) in enumerate(self.entries) if c.sha == b.sha), -1)
        if row >= 0:
            self.tabs.setCurrentIndex(0)
            self.list.setCurrentRow(row)
