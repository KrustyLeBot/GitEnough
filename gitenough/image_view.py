"""Images in the Changes window: the picture itself instead of "Binary files differ", before and after side by side."""

import os

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .git_ops import GitError, run_git_bytes
from .repo import FileChange
from .style import C

# What Qt can decode here; SVG stays a text diff (it is text, and the SVG plugin is not shipped).
IMAGE_EXTS = {"." + bytes(f).decode().lower() for f in QImageReader.supportedImageFormats()} - {".svg", ".svgz"}
MAX_IMAGE_BYTES = 40_000_000


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def _git_blob(repo: str, spec: str) -> bytes | None:
    try:
        return run_git_bytes(["show", spec], cwd=repo, timeout=60)
    except GitError:
        return None


def _disk(repo: str, rel: str) -> bytes | None:
    full = os.path.join(repo, rel)
    try:
        if os.path.getsize(full) > MAX_IMAGE_BYTES:
            return None
        with open(full, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def load_pair(repo: str, fc: FileChange) -> tuple[bytes | None, bytes | None]:
    """(before, after) bytes of an image change, as the Changes diff compares them (worker thread)."""
    old_path = fc.orig or fc.path
    if fc.staged:  # HEAD -> index
        before = None if fc.code == "A" else _git_blob(repo, f"HEAD:{old_path}")
        after = None if fc.code == "D" else _git_blob(repo, f":{fc.path}")
    else:  # index -> working folder
        before = None if fc.code == "?" else _git_blob(repo, f":{old_path}")
        after = None if fc.code == "D" else _disk(repo, fc.path)
    return before, after


class _Picture(QWidget):
    """An image scaled to fit (never enlarged past 2x), on a checkerboard that shows transparency."""

    def __init__(self):
        super().__init__()
        self.image = QImage()
        self.setMinimumSize(80, 80)
        tile = QPixmap(16, 16)
        tile.fill(QColor("#1a1d24"))
        p = QPainter(tile)
        p.fillRect(0, 0, 8, 8, QColor("#23272f"))
        p.fillRect(8, 8, 8, 8, QColor("#23272f"))
        p.end()
        self.checker = QBrush(tile)

    def set_image(self, image: QImage):
        self.image = image
        self.update()

    def paintEvent(self, _event):
        if self.image.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        size = QSize(self.image.size())
        limit = QSize(min(self.width() - 16, size.width() * 2), min(self.height() - 16, size.height() * 2))
        size.scale(limit, Qt.KeepAspectRatio)
        x, y = (self.width() - size.width()) / 2, (self.height() - size.height()) / 2
        target = QRectF(x, y, size.width(), size.height())
        p.fillRect(target, self.checker)
        p.drawImage(target, self.image)
        p.setPen(QColor(C["border"]))
        p.drawRect(target.adjusted(-0.5, -0.5, 0.5, 0.5))


class _Side(QWidget):
    def __init__(self, title: str, color: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)
        self.caption = QLabel(title)
        self.caption.setStyleSheet(f"color: {color}; font-weight: 700;")
        self.info = QLabel("")
        self.info.setObjectName("muted")
        head = QHBoxLayout()
        head.addWidget(self.caption)
        head.addWidget(self.info, 1)
        lay.addLayout(head)
        self.picture = _Picture()
        lay.addWidget(self.picture, 1)

    def show_data(self, data: bytes | None):
        image = QImage.fromData(data) if data else QImage()
        self.picture.set_image(image)
        if data is None:
            self.info.setText("")
        elif image.isNull():
            self.info.setText(f"· {len(data) / 1024:,.0f} KB · cannot be shown")
        else:
            self.info.setText(f"· {image.width()} × {image.height()} px · {len(data) / 1024:,.1f} KB")


class ImageDiffView(QWidget):
    """Before and after of one image; a single side for an added or deleted one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        header = QWidget()
        header.setObjectName("diffHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 8, 10, 8)
        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: 700; font-size: 10.5pt;")
        self.note = QLabel("")
        self.note.setObjectName("muted")
        hl.addWidget(self.title, 1)
        hl.addWidget(self.note)
        lay.addWidget(header)
        sides = QHBoxLayout()
        sides.setSpacing(0)
        self.before = _Side("Before", C["red"])
        self.after = _Side("After", C["green"])
        sides.addWidget(self.before, 1)
        sides.addWidget(self.after, 1)
        lay.addLayout(sides, 1)

    def show_pair(self, path: str, before: bytes | None, after: bytes | None, before_label: str, after_label: str):
        self.title.setText(path)
        self.before.caption.setText(f"Before · {before_label}")
        self.after.caption.setText(f"After · {after_label}")
        self.before.setVisible(before is not None)
        self.after.setVisible(after is not None)
        self.before.show_data(before)
        self.after.show_data(after)
        self.note.setText("New image" if before is None else "Deleted image" if after is None
                          else "Unchanged pixels, other bytes" if before != after and _same_pixels(before, after)
                          else "")


def _same_pixels(a: bytes, b: bytes) -> bool:
    ia, ib = QImage.fromData(a), QImage.fromData(b)
    return not ia.isNull() and ia.convertToFormat(QImage.Format_ARGB32) == ib.convertToFormat(QImage.Format_ARGB32)
