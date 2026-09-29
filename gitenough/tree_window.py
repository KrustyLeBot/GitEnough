import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMenu, QPushButton, QScrollArea, QSplitter, QStyle,
                               QStyledItemDelegate, QTableView, QVBoxLayout, QWidget)

from . import repo
from .changes_window import tree_toggle
from .diff_view import DiffView, parse_diff
from .repo import Commit, FileChange, History
from .style import C
from .file_view import ExtensionBar, FileView, extension
from .widgets import icon_button, keep_size

LANE_W = 14
ROW_H = 28
PAGE = 1000
LANE_COLORS = ["#7c8cff", "#3ecf8e", "#f5a524", "#f47fb4", "#5aa9ff", "#b392f0", "#f26b6b", "#4fd1c5"]
REF_COLORS = {"head": C["green"], "local": C["accent"], "remote": "#7aa2c8", "tag": C["yellow"],
              "detached": C["violet"]}


@dataclass(slots=True)
class GraphRow:
    col: int
    color: int
    width: int
    # (x1, y1, x2, y2, color): lanes as x, row fraction (0 top, 0.5 node, 1 bottom) as y
    segments: list = field(default_factory=list)


def build_graph(commits: list[Commit]) -> list[GraphRow]:
    lanes: list[str | None] = []
    colors: list[int] = []
    next_color = 0
    rows = []

    def free_slot() -> int:
        if None in lanes:
            return lanes.index(None)
        lanes.append(None)
        colors.append(0)
        return len(lanes) - 1

    for c in commits:
        segs = []
        if c.sha in lanes:
            col = lanes.index(c.sha)
        else:
            col = free_slot()
            colors[col] = next_color
            next_color += 1
        color = colors[col]
        for i, sha in enumerate(lanes):
            if sha is None:
                continue
            if sha == c.sha:
                segs.append((i, 0.0, col, 0.5, colors[i]))  # Children converge into this commit.
            else:
                segs.append((i, 0.0, i, 1.0, colors[i]))  # Unrelated lane passes through.
        for i, sha in enumerate(lanes):
            if sha == c.sha:
                lanes[i] = None
        for n, parent in enumerate(c.parents):
            if parent in lanes:
                j = lanes.index(parent)
            elif n == 0:
                j = col
                lanes[col], colors[col] = parent, color
            else:
                j = free_slot()
                lanes[j], colors[j] = parent, next_color
                next_color += 1
            segs.append((col, 0.5, j, 1.0, colors[j]))
        while lanes and lanes[-1] is None:
            lanes.pop()
            colors.pop()
        width = max([col] + [max(s[0], s[2]) for s in segs]) + 1
        rows.append(GraphRow(col, color, width, segs))
    return rows


def parse_refs(refs: list[str], remotes: list[str]) -> list[tuple[str, str]]:
    out = []
    for r in refs:
        if r.startswith("HEAD -> "):
            out.append((r[8:], "head"))
        elif r == "HEAD":
            out.append(("HEAD", "detached"))
        elif r.startswith("tag: "):
            out.append((r[5:], "tag"))
        elif any(r.startswith(f"{rm}/") for rm in remotes):
            if not r.endswith("/HEAD"):
                out.append((r, "remote"))
        else:
            out.append((r, "local"))
    return out


def rel_time(ts: int) -> str:
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 86400 * 30:
        days = int(d // 86400)
        return f"{days} day{'s' if days > 1 else ''} ago"
    return time.strftime("%d %b %Y", time.localtime(ts))


def _load(path: str, scope: str, limit: int, base: str):
    hist = repo.history(path, scope, limit, base)
    return hist, build_graph(hist.commits)


def _details(path: str, c: Commit):
    # Two independent git calls: run them side by side (process start-up dominates on Windows).
    with ThreadPoolExecutor(max_workers=1) as pool:
        body = pool.submit(repo.commit_body, path, c.sha)
        files = repo.commit_files(path, c)
        return body.result(), files


class HistoryModel(QAbstractTableModel):
    HEADERS = ["Message", "Author", "Date", "Commit"]

    def __init__(self):
        super().__init__()
        self.hist = History()
        self.graph: list[GraphRow] = []
        self.refs: list[list[tuple[str, str]]] = []
        self.rows: list[int] = []
        self.filtered = False

    def load(self, hist: History, graph: list[GraphRow]):
        self.beginResetModel()
        self.hist, self.graph = hist, graph
        self.refs = [parse_refs(c.refs, hist.remotes) for c in hist.commits]
        self.rows = list(range(len(hist.commits)))
        self.filtered = False
        self.endResetModel()

    def set_filter(self, term: str):
        term = term.strip().lower()
        self.beginResetModel()
        self.filtered = bool(term)
        commits = self.hist.commits
        self.rows = [i for i, c in enumerate(commits)
                     if not term or term in c.subject.lower() or term in c.author.lower()
                     or c.sha.startswith(term) or any(term in r.lower() for r in c.refs)]
        self.endResetModel()

    def commit(self, row: int) -> Commit:
        return self.hist.commits[self.rows[row]]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 4

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        i = self.rows[index.row()]
        c = self.hist.commits[i]
        col = index.column()
        if role == Qt.UserRole:
            return c, (None if self.filtered else self.graph[i]), self.refs[i], c.sha == self.hist.head
        if role == Qt.DisplayRole:
            return (c.subject, c.author, rel_time(c.time), c.sha[:8])[col]
        if role == Qt.ToolTipRole:
            if col == 2:
                return time.strftime("%Y-%m-%d %H:%M", time.localtime(c.time))
            if col == 1:
                return c.email
            if col == 0:
                return c.subject
        if role == Qt.ForegroundRole and col:
            return QColor(C["muted"] if col != 3 else C["faint"])
        if role == Qt.FontRole and col == 3:
            font = QFont("Cascadia Mono")
            font.setStyleHint(QFont.Monospace)
            font.setPointSizeF(9)
            return font
        return None


class GraphDelegate(QStyledItemDelegate):
    def paint(self, p: QPainter, option, index):
        c, graph, refs, is_head = index.data(Qt.UserRole)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect
        x0 = r.left() + 10
        top, h = r.top(), r.height()
        if graph is not None:
            for x1, y1, x2, y2, color in graph.segments:
                pen = QPen(QColor(LANE_COLORS[color % len(LANE_COLORS)]), 2)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                a = QPointF(x0 + x1 * LANE_W + LANE_W / 2, top + y1 * h)
                b = QPointF(x0 + x2 * LANE_W + LANE_W / 2, top + y2 * h)
                if x1 == x2:
                    p.drawLine(a, b)
                else:
                    mid = (a.y() + b.y()) / 2
                    path = QPainterPath(a)
                    path.cubicTo(QPointF(a.x(), mid), QPointF(b.x(), mid), b)
                    p.drawPath(path)
            center = QPointF(x0 + graph.col * LANE_W + LANE_W / 2, top + h / 2)
            color = QColor(LANE_COLORS[graph.color % len(LANE_COLORS)])
            text_x = x0 + graph.width * LANE_W + 8
        else:
            center = QPointF(x0 + LANE_W / 2, top + h / 2)
            color = QColor(C["accent"])
            text_x = x0 + LANE_W + 8
        if is_head:
            p.setPen(QPen(QColor(C["text"]), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(center, 7, 7)
        if len(c.parents) > 1:
            p.setPen(QPen(color, 2))
            p.setBrush(QColor("#15181f"))
            p.drawEllipse(center, 3.6, 3.6)
        else:
            p.setPen(QPen(QColor("#15181f"), 1.5))
            p.setBrush(color)
            p.drawEllipse(center, 4.8, 4.8)

        badge_font = QFont(option.font)
        badge_font.setPointSizeF(8)
        badge_font.setBold(True)
        p.setFont(badge_font)
        fm = p.fontMetrics()
        x = text_x
        for text, kind in refs:
            w = fm.horizontalAdvance(text) + 14
            if x + w > r.right() - 120:
                break
            box = QRectF(x, top + (h - 18) / 2, w, 18)
            col = QColor(REF_COLORS[kind])
            fill = QColor(col)
            fill.setAlpha(34)
            p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 90), 1))
            p.setBrush(fill)
            p.drawRoundedRect(box, 9, 9)
            p.setPen(col)
            p.drawText(box, Qt.AlignCenter, text)
            x += w + 6

        font = QFont(option.font)
        if is_head:
            font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(C["muted"] if len(c.parents) > 1 else C["text"]))
        rect = QRectF(x + 2, top, r.right() - x - 8, h)
        p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(c.subject, Qt.ElideRight, int(rect.width())))
        p.restore()


class RowBackground(QStyledItemDelegate):
    """Plain columns: paint selection/hover consistently with the graph column."""

    def paint(self, p, option, index):
        option.state &= ~QStyle.State_HasFocus
        super().paint(p, option, index)


class TreeWindow(QWidget):
    def __init__(self, main, project):
        super().__init__(main, Qt.Window)
        self.main, self.p, self.tasks = main, project, main.tasks
        self.path = project.path
        self.limit = PAGE
        self.commit: Commit | None = None
        self.file: FileChange | None = None
        self.last_refresh = 0.0

        self.setWindowTitle(f"History · {project.name}")
        self.resize(1440, 900)
        keep_size(self, self.main.config, "history")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 14, 16, 14)
        title = QLabel(project.name)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        hl.addWidget(title)
        hl.addSpacing(10)
        base = project.snap.base if project.snap else ""
        self.base = base
        self.scope = QComboBox()
        self.scope.addItem(f"Current + {base}" if base else "Current + upstream", "base")
        self.scope.addItem("Current branch", "current")
        self.scope.addItem("All branches", "all")
        self.scope.setToolTip("Which branches the graph shows")
        self.scope.currentIndexChanged.connect(self.refresh)
        hl.addWidget(self.scope)
        hl.addStretch()
        self.count = QLabel("")
        self.count.setObjectName("muted")
        hl.addWidget(self.count)
        self.more = QPushButton("Load more")
        self.more.setObjectName("rowAction")
        self.more.hide()
        self.more.clicked.connect(self.load_more)
        hl.addWidget(self.more)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter: message, author, hash, branch  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(320)
        self.search.textChanged.connect(self.on_filter)
        hl.addWidget(self.search)
        refresh = icon_button("refresh", "Refresh (F5)")
        refresh.clicked.connect(self.refresh)
        bash = icon_button("terminal", "Open Git Bash here")
        bash.clicked.connect(lambda: self.main.open_bash(self.path))
        hl.addWidget(refresh)
        hl.addWidget(bash)
        root.addWidget(header)

        self.model = HistoryModel()
        self.table = QTableView()
        self.table.setObjectName("history")
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(0, GraphDelegate(self.table))
        rb = RowBackground(self.table)
        for col in (1, 2, 3):
            self.table.setItemDelegateForColumn(col, rb)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(ROW_H)
        self.table.verticalHeader().setMinimumSectionSize(ROW_H)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setMouseTracking(True)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in ((1, 170), (2, 120), (3, 90)):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
            self.table.setColumnWidth(col, w)
        self.table.selectionModel().currentRowChanged.connect(self.on_commit)

        details = QWidget()
        details.setObjectName("sidePanel")
        dl = QVBoxLayout(details)
        dl.setContentsMargins(16, 14, 12, 12)
        dl.setSpacing(8)
        self.info = QLabel("Select a commit")
        self.info.setObjectName("commitInfo")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.info)
        scroll.setObjectName("plainScroll")
        dl.addWidget(scroll, 2)
        copy = QPushButton("Copy hash")
        copy.setObjectName("rowAction")
        copy.clicked.connect(lambda: self.commit and QGuiApplication.clipboard().setText(self.commit.sha))
        files_head = QHBoxLayout()
        self.files_title = QLabel("Files")
        self.files_title.setObjectName("sectionTitle")
        files_head.addWidget(self.files_title)
        files_head.addStretch()
        files_head.addWidget(copy)
        self.files = FileView(main.config.file_tree)
        self.files.selection_changed.connect(self.on_file)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self.file_menu)
        files_head.addWidget(tree_toggle(main, [self.files]))
        dl.addLayout(files_head)
        self.ext_bar = ExtensionBar()
        self.ext_bar.toggled.connect(self.set_ext)
        self.ext = ""
        self.all_files: list[FileChange] = []
        dl.addWidget(self.ext_bar)
        dl.addWidget(self.files, 3)

        self.diff = DiffView()
        self.diff.set_message("Select a file to see its diff")
        self.diff.options_changed.connect(lambda: self.file and self.show_diff(self.file))

        bottom = QSplitter(Qt.Horizontal)
        bottom.setHandleWidth(1)
        bottom.addWidget(details)
        bottom.addWidget(self.diff)
        bottom.setSizes([420, 1020])
        vsplit = QSplitter(Qt.Vertical)
        vsplit.setHandleWidth(1)
        vsplit.addWidget(self.table)
        vsplit.addWidget(bottom)
        vsplit.setSizes([420, 480])
        root.addWidget(vsplit, 1)

        QShortcut(QKeySequence("F5"), self, self.refresh)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.search.setFocus())
        self.refresh()

    def refresh(self):
        self.last_refresh = time.monotonic()
        self.count.setText("Loading…")
        self.tasks.submit(_load, self._on_load, self.path, self.scope.currentData(), self.limit, self.base)

    def load_more(self):
        self.limit += PAGE
        self.refresh()

    def _on_load(self, result, error):
        if error:
            self.count.setText("Could not read history")
            self.info.setText(f"<span style='color:{C['red']}'>{error}</span>")
            return
        hist, graph = result
        n = len(hist.commits)
        self.count.setText(f"{n} commit{'s' if n != 1 else ''}" + ("+" if n >= self.limit else ""))
        self.more.setVisible(n >= self.limit)
        old = self.model.hist
        if (old.head == hist.head and [(c.sha, c.refs) for c in old.commits]
                == [(c.sha, c.refs) for c in hist.commits]):
            return  # Nothing changed: keep selection and scroll position untouched.
        keep = self.commit.sha if self.commit else None
        scroll = self.table.verticalScrollBar().value()
        first_load = not old.commits
        self.model.load(hist, graph)
        self.model.set_filter(self.search.text())
        target = next((i for i in range(self.model.rowCount()) if self.model.commit(i).sha == keep), None)
        if target is not None and not first_load:
            self.table.selectRow(target)
            self.table.verticalScrollBar().setValue(scroll)
            return
        head = next((i for i in range(self.model.rowCount()) if self.model.commit(i).sha == hist.head), 0)
        if self.model.rowCount():
            self.table.selectRow(head)
            self.table.scrollTo(self.model.index(head, 0), QAbstractItemView.PositionAtCenter)

    def on_filter(self, text: str):
        keep = self.commit.sha if self.commit else None
        self.model.set_filter(text)
        for i in range(self.model.rowCount()):
            if self.model.commit(i).sha == keep:
                self.table.selectRow(i)
                break

    def on_commit(self, current: QModelIndex, _previous=None):
        if not current.isValid():
            return
        c = self.model.commit(current.row())
        if self.commit and c.sha == self.commit.sha:
            return
        self.commit, self.file = c, None
        self.files.set_files([])
        self.diff.set_message("Select a file to see its diff")
        self.tasks.submit(_details, lambda res, err: self._on_details(c, res, err), self.path, c)

    def _on_details(self, c: Commit, result, error):
        if not self.commit or self.commit.sha != c.sha:
            return
        if error:
            self.info.setText(f"<span style='color:{C['red']}'>{error}</span>")
            return
        body, files = result
        esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
        subject, _, rest = body.partition("\n")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.time))
        parents = ", ".join(p[:8] for p in c.parents) or "none (root commit)"
        self.info.setText(
            f"<div style='font-size:11.5pt; font-weight:700; color:{C['text']}'>{esc(subject)}</div>"
            + (f"<pre style='white-space:pre-wrap; font-family:Segoe UI; color:{C['muted']}; margin-top:8px'>"
               f"{esc(rest.strip())}</pre>" if rest.strip() else "")
            + f"<table style='margin-top:10px; color:{C['muted']}' cellspacing='0' cellpadding='2'>"
            f"<tr><td style='color:{C['faint']}; padding-right:10px'>Author</td><td>{esc(c.author)} "
            f"&lt;{esc(c.email)}&gt;</td></tr>"
            f"<tr><td style='color:{C['faint']}'>Date</td><td>{when} ({rel_time(c.time)})</td></tr>"
            f"<tr><td style='color:{C['faint']}'>Commit</td><td style='font-family:Consolas'>{c.sha}</td></tr>"
            f"<tr><td style='color:{C['faint']}'>Parents</td><td style='font-family:Consolas'>{parents}</td></tr>"
            "</table>")
        n = len(files)
        suffix = " (vs first parent)" if len(c.parents) > 1 else ""
        self.files_title.setText(f"{n} file{'s' if n != 1 else ''} changed{suffix}")
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

    def file_menu(self, pos):
        files = self.files.item_files_at(pos)
        if len(files) != 1 or files[0].code == "D":
            return
        menu = QMenu(self)
        act = QAction("File history && blame", menu)
        act.triggered.connect(lambda: self.main.open_file_history(self.p, files[0].path))
        menu.addAction(act)
        menu.exec(self.files.viewport().mapToGlobal(pos))

    def show_diff(self, fc: FileChange):
        c = self.commit
        self.diff.set_message("Loading…", fc.path)
        ws, full = self.diff.ignore_ws, self.diff.full_file
        self.tasks.submit(lambda: parse_diff(repo.commit_diff(self.path, c, fc, ws, full)),
                          lambda data, err: self._on_diff(c, fc, data, err))

    def _on_diff(self, c: Commit, fc: FileChange, data, error):
        if self.commit is not c or self.file is not fc:
            return
        if error:
            self.diff.set_message(str(error), fc.path)
        else:
            self.diff.set_diff(fc.path, data)

    def changeEvent(self, event):
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow()
                and time.monotonic() - self.last_refresh > 30):
            self.refresh()
        super().changeEvent(event)
