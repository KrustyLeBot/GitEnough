"""File list shown as a flat list or a collapsible folder tree, plus an extension summary."""

from collections import Counter

from PySide6.QtCore import QEvent, QItemSelectionModel, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QAbstractItemView, QLabel, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QTreeWidget, QTreeWidgetItem)

from .repo import FileChange
from .style import C, chevron_pixmap
from .widgets import CODE_COLORS

DIR = "dir"
CLEAR = "clear-filter"


def extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if "." not in name.lstrip("."):
        return "(none)"
    return "." + name.rsplit(".", 1)[-1].lower()


class _Delegate(QStyledItemDelegate):
    ROW = 28
    BOX = 14  # check box size, when the view has check boxes

    def __init__(self, view: "FileView"):
        super().__init__(view)
        self.view = view

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.ROW)

    def paint(self, p: QPainter, option, index):
        data = index.data(Qt.UserRole)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(2, 1, -4, -1)
        if self.view.selectionModel().isSelected(index):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#262c3a"))
            p.drawRoundedRect(r, 6, 6)
        elif option.state & QStyle.State_MouseOver:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["hover"]))
            p.drawRoundedRect(r, 6, 6)
        if self.view.checks is not None:
            state = self.view.check_state(self.view.itemFromIndex(index))
            self._paint_check(p, QRectF(r.left() + 6, r.center().y() - self.BOX / 2, self.BOX, self.BOX), state)
            r = r.adjusted(self.BOX + 6, 0, 0, 0)
        if isinstance(data, tuple) and data[0] == DIR:
            self._paint_dir(p, option, r, index, data)
        elif isinstance(data, FileChange):
            self._paint_file(p, option, r, data)
        p.restore()

    @staticmethod
    def _paint_check(p, box: QRectF, state):
        on = state != Qt.Unchecked
        p.setPen(QPen(QColor(C["accent"] if on else "#3a4150"), 1.4))
        p.setBrush(QColor(C["accent"] if state == Qt.Checked else C["surface"]))
        p.drawRoundedRect(box, 4, 4)
        if state == Qt.Checked:
            p.setPen(QPen(QColor("#0b0d12"), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            x, y, s = box.left(), box.top(), box.width()
            p.drawPolyline([QPointF(x + s * 0.25, y + s * 0.52), QPointF(x + s * 0.43, y + s * 0.70),
                            QPointF(x + s * 0.76, y + s * 0.32)])
        elif state == Qt.PartiallyChecked:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["accent"]))
            p.drawRoundedRect(box.adjusted(4, 4, -4, -4), 1.5, 1.5)

    def _paint_dir(self, p, option, r, index, data):
        _, path, count = data
        x = r.left() + 6
        folder = QRectF(x, r.center().y() - 6, 16, 12)
        p.setPen(QPen(QColor(C["faint"]), 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(folder, 2, 2)
        p.drawLine(folder.topLeft() + QRectF(0, 0, 6, 0).topRight(), folder.topLeft())
        font = QFont(option.font)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        p.setPen(QColor(C["muted"]))
        label = index.data(Qt.DisplayRole)
        text_rect = QRectF(x + 24, r.top(), r.right() - x - 70, r.height())
        shown = p.fontMetrics().elidedText(label, Qt.ElideMiddle, int(text_rect.width()))
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, shown)
        small = QFont(option.font)
        small.setPointSizeF(8.5)
        p.setFont(small)
        p.setPen(QColor(C["faint"]))
        p.drawText(QRectF(r.right() - 44, r.top(), 38, r.height()), Qt.AlignVCenter | Qt.AlignRight, str(count))

    def _paint_file(self, p, option, r, fc: FileChange):
        color = QColor(CODE_COLORS.get(fc.code, C["muted"]))
        badge = QRectF(r.left() + 6, r.center().y() - 8, 18, 16)
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
        folder = "" if self.view.tree_mode else fc.path[: -len(name)].rstrip("/")
        if fc.orig:
            folder = f"from {fc.orig}" if self.view.tree_mode else f"{fc.orig}  →  {folder or '.'}"
        x = badge.right() + 10
        right = r.right() - 6
        pill = self.view.badges.get(fc.path)
        if pill:
            small = QFont(option.font)
            small.setPointSizeF(7.5)
            small.setBold(True)
            p.setFont(small)
            w = p.fontMetrics().horizontalAdvance(pill[0]) + 12
            box = QRectF(right - w, r.center().y() - 8, w, 16)
            tint = QColor(pill[1])
            tint.setAlpha(46)
            p.setPen(Qt.NoPen)
            p.setBrush(tint)
            p.drawRoundedRect(box, 8, 8)
            p.setPen(QColor(pill[1]))
            p.drawText(box, Qt.AlignCenter, pill[0])
            right = box.left() - 6
        font = QFont(option.font)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        dim = self.view.dim_checked and self.view.checks is not None and fc.path in self.view.checks
        p.setPen(QColor(C["faint"] if dim else C["text"]))
        text_rect = QRectF(x, r.top(), right - x, r.height())
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


class FileView(QTreeWidget):
    """Files as a flat list or as folders; selecting a folder selects every file below it."""

    selection_changed = Signal()
    activated = Signal(list)  # double-click / Space / Enter on files
    delete_pressed = Signal(list)
    check_toggled = Signal(list, bool)  # files under the clicked box, new state

    def __init__(self, tree_mode: bool = False, parent=None):
        super().__init__(parent)
        self.tree_mode = tree_mode
        # Checked paths, shared with the owner (which updates it on check_toggled); None: no check boxes.
        self.checks: set[str] | None = None
        self.badges: dict[str, tuple[str, str]] = {}  # path -> (text, color) drawn at the row's right
        self.dim_checked = False  # checked rows drawn muted (files marked as viewed)
        self.files: list[FileChange] = []
        self._collapsed: set[str] = set()
        self.setObjectName("fileView")
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setItemDelegate(_Delegate(self))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setUniformRowHeights(True)
        self.setIndentation(16)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setExpandsOnDoubleClick(True)
        # The delegate paints selection itself; the style would also paint the indentation in the highlight color.
        pal = self.palette()
        pal.setColor(QPalette.Highlight, Qt.transparent)
        self.setPalette(pal)
        self.itemSelectionChanged.connect(self.selection_changed)
        self.itemDoubleClicked.connect(self._double_clicked)
        self.itemCollapsed.connect(lambda item: self._collapsed.add(self._dir_path(item)))
        self.itemExpanded.connect(lambda item: self._collapsed.discard(self._dir_path(item)))

    def drawRow(self, painter, option, index):
        # The native style draws an accent bar on selected rows; the delegate draws selection instead.
        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        super().drawRow(painter, opt, index)

    def drawBranches(self, painter, rect, index):
        # Only the expand chevron: the native Windows 11 style also paints selection bars in the indentation.
        item = self.itemFromIndex(index)
        if item is None or not item.childCount():
            return
        if not hasattr(self, "_chevrons"):
            self._chevrons = {True: chevron_pixmap("down"), False: chevron_pixmap("right")}
        pm = self._chevrons[item.isExpanded()]
        x = rect.right() - self.indentation() + (self.indentation() - pm.width()) // 2 + 1
        painter.drawPixmap(x, rect.top() + (rect.height() - pm.height()) // 2, pm)

    # ---------- content ----------
    def set_tree_mode(self, tree: bool):
        if tree != self.tree_mode:
            keep = self.current_file()
            self.tree_mode = tree
            self.set_files(self.files, keep.key if keep else None)

    def set_files(self, files: list[FileChange], keep_key=None):
        scroll = self.verticalScrollBar().value()
        selected = {f.key for f in self.selected_files()} if keep_key is None else {keep_key}
        self.files = list(files)
        self.blockSignals(True)
        self.clear()
        self.setRootIsDecorated(self.tree_mode)
        if self.tree_mode:
            self._build_tree(files)
        else:
            for fc in files:
                self.addTopLevelItem(self._file_item(fc))
        current = None
        for item in self._file_items():
            fc = item.data(0, Qt.UserRole)
            if fc.key in selected:
                item.setSelected(True)
                if current is None or fc.key == keep_key:
                    current = item
        if current is not None:
            # NoUpdate: move the current item without touching the restored selection.
            self.setCurrentItem(current, 0, QItemSelectionModel.NoUpdate)
        self.blockSignals(False)
        self.verticalScrollBar().setValue(scroll)

    def _file_item(self, fc: FileChange) -> QTreeWidgetItem:
        item = QTreeWidgetItem([fc.path])
        item.setData(0, Qt.UserRole, fc)
        item.setToolTip(0, fc.path + (f"\nfrom {fc.orig}" if fc.orig else ""))
        return item

    def _build_tree(self, files: list[FileChange]):
        root: dict = {}
        for fc in files:
            node = root
            for part in fc.path.split("/")[:-1]:
                node = node.setdefault(part, {})
            node.setdefault(None, []).append(fc)

        def count(node) -> int:
            return len(node.get(None, [])) + sum(count(v) for k, v in node.items() if k is not None)

        def add(parent, node, prefix: str):
            for name in sorted((k for k in node if k is not None), key=str.lower):
                child, label = node[name], name
                # Single-folder chains are merged ("src/App/Models") so the tree stays shallow.
                while len(child) == 1 and None not in child:
                    only = next(iter(child))
                    label, child = f"{label}/{only}", child[only]
                path = f"{prefix}{label}"
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.UserRole, (DIR, path, count(child)))
                item.setToolTip(0, path)
                (parent.addChild if parent else self.addTopLevelItem)(item)
                add(item, child, path + "/")
                item.setExpanded(path not in self._collapsed)
            for fc in sorted(node.get(None, []), key=lambda f: f.path.lower()):
                (parent.addChild if parent else self.addTopLevelItem)(self._file_item(fc))

        add(None, root, "")

    @staticmethod
    def _dir_path(item) -> str:
        data = item.data(0, Qt.UserRole)
        return data[1] if isinstance(data, tuple) else ""

    def _file_items(self):
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop(0)
            if isinstance(item.data(0, Qt.UserRole), FileChange):
                yield item
            stack[0:0] = [item.child(i) for i in range(item.childCount())]

    # ---------- queries ----------
    def file_count(self) -> int:
        return sum(1 for _ in self._file_items())

    def _files_under(self, item) -> list[FileChange]:
        data = item.data(0, Qt.UserRole)
        if isinstance(data, FileChange):
            return [data]
        out = []
        for i in range(item.childCount()):
            out += self._files_under(item.child(i))
        return out

    def selected_files(self) -> list[FileChange]:
        seen, out = set(), []
        for item in self.selectedItems():
            for fc in self._files_under(item):
                if fc.key not in seen:
                    seen.add(fc.key)
                    out.append(fc)
        return out

    def current_file(self) -> FileChange | None:
        item = self.currentItem()
        data = item.data(0, Qt.UserRole) if item else None
        return data if isinstance(data, FileChange) else None

    def select_path(self, path: str) -> bool:
        """Make a file current without rebuilding the list (a rebuild of 400 rows costs ~15 ms)."""
        item = next((it for it in self._file_items() if it.data(0, Qt.UserRole).path == path), None)
        if item is None:
            return False
        self.blockSignals(True)
        self.clearSelection()
        self.setCurrentItem(item)
        item.setSelected(True)
        self.blockSignals(False)
        self.scrollToItem(item)
        self.viewport().update()
        return True

    def select_first(self) -> bool:
        first = next(self._file_items(), None)
        if first is None:
            return False
        self.clearSelection()
        self.setCurrentItem(first)
        first.setSelected(True)
        return True

    def item_files_at(self, pos) -> list[FileChange]:
        item = self.itemAt(pos)
        if item is None:
            return []
        if not item.isSelected():
            self.clearSelection()
            item.setSelected(True)
            self.setCurrentItem(item)
        return self.selected_files()

    def check_state(self, item):
        files = self._files_under(item)
        hits = sum(1 for f in files if f.path in self.checks)
        return Qt.Unchecked if not hits else Qt.Checked if hits == len(files) else Qt.PartiallyChecked

    # ---------- input ----------
    def _box_item(self, pos):
        """The item whose check box is under pos, else None."""
        item = self.itemAt(pos)
        if self.checks is None or item is None:
            return None
        left = self.visualItemRect(item).left() + 2
        return item if left + 2 <= pos.x() <= left + 8 + _Delegate.BOX + 2 else None

    def mousePressEvent(self, event):
        item = self._box_item(event.position().toPoint())
        if item is not None and event.button() == Qt.LeftButton:
            # Checking is independent from selection: the diff on screen stays.
            self.check_toggled.emit(self._files_under(item), self.check_state(item) != Qt.Checked)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._box_item(event.position().toPoint()) is not None:
            self.mousePressEvent(event)  # A fast second click is a second toggle, not a stage.
            return
        super().mouseDoubleClickEvent(event)

    def _double_clicked(self, item, _col):
        data = item.data(0, Qt.UserRole)
        if isinstance(data, FileChange):
            self.activated.emit([data])

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.activated.emit(self.selected_files())
            return
        if event.key() == Qt.Key_Delete:
            self.delete_pressed.emit(self.selected_files())
            return
        super().keyPressEvent(event)


class ExtensionBar(QLabel):
    """Clickable per-extension counts (".cs 12 · .json 3"); clicking one filters the lists."""

    toggled = Signal(str)  # extension, or "" to clear

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("extBar")
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        # An empty href is not clickable in Qt: the active extension links to CLEAR to switch the filter off.
        self.linkActivated.connect(lambda ext: self.toggled.emit("" if ext == CLEAR else ext))
        self.hide()

    def set_files(self, files: list[FileChange], active: str):
        counts = Counter(extension(f.path) for f in files)
        if not counts:
            self.hide()
            return
        parts = []
        for ext, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            on = ext == active
            color = C["text"] if on else C["muted"]
            bg = "background-color:#2a3152;" if on else ""
            parts.append(f"<a href='{CLEAR if on else ext}' style='color:{color}; text-decoration:none; {bg}'>"
                         f"&nbsp;{ext}&nbsp;<span style='color:{C['faint']}'>{n}</span>&nbsp;</a>")
        self.setText("&nbsp;".join(parts))
        self.setToolTip("Files by extension. Click one to show only those files, click it again to show all.")
        self.show()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.EnabledChange:
            self.update()
