"""About page: name, version, tagline and the David Goodenough seal of approval."""

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import __version__
from .style import C, app_icon

REPO_URL = "https://github.com/KrustyLeBot/GitEnough"
GOLD = QColor("#f2c94c")
GOLD_DARK = QColor("#b8902a")


class GoodEnoughSeal(QWidget):
    """A rosette seal: "Certified good enough by David Goodenough"."""

    def sizeHint(self) -> QSize:
        return QSize(230, 250)

    def minimumSizeHint(self) -> QSize:
        return QSize(230, 250)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy, r = self.width() / 2, 105, 88

        # Ribbon tails behind the seal.
        for side in (-1, 1):
            tail = QPainterPath(QPointF(cx + side * 22, cy + 50))
            tail.lineTo(cx + side * 62, cy + 128)
            tail.lineTo(cx + side * 38, cy + 118)
            tail.lineTo(cx + side * 28, cy + 142)
            tail.lineTo(cx + side * 2, cy + 64)
            tail.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["accent"]).darker(115) if side < 0 else QColor(C["accent"]))
            p.drawPath(tail)

        # Serrated rosette edge.
        edge = QPainterPath()
        spikes = 36
        for i in range(spikes * 2):
            angle = math.pi * i / spikes
            radius = r if i % 2 == 0 else r - 8
            point = QPointF(cx + radius * math.cos(angle), cy + radius * math.sin(angle))
            edge.moveTo(point) if i == 0 else edge.lineTo(point)
        edge.closeSubpath()
        p.setBrush(GOLD)
        p.setPen(QPen(GOLD_DARK, 1.5))
        p.drawPath(edge)

        p.setBrush(QColor(C["surface"]))
        p.setPen(QPen(GOLD, 3))
        p.drawEllipse(QPointF(cx, cy), r - 16, r - 16)
        p.setPen(QPen(GOLD_DARK, 1, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r - 23, r - 23)

        def text(y: float, value: str, size: float, color: QColor, bold: bool = True, spacing: float = 0):
            font = QFont(self.font())
            font.setPointSizeF(size)
            font.setBold(bold)
            font.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRectF(cx - r, y, 2 * r, 24), Qt.AlignCenter, value)

        text(cy - 58, "CERTIFIED", 7.5, GOLD, spacing=2.5)
        # Big check mark.
        check = QPainterPath(QPointF(cx - 20, cy - 16))
        check.lineTo(cx - 6, cy - 2)
        check.lineTo(cx + 22, cy - 32)
        p.setPen(QPen(QColor(C["green"]), 7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(check)
        text(cy + 4, "GOOD ENOUGH", 10.5, QColor(C["text"]), spacing=0.5)
        text(cy + 24, "by David Goodenough", 7, QColor(C["muted"]), bold=False)
        p.end()


class AboutPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(app_icon(56))
        head.addWidget(logo, 0, Qt.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        name = QLabel("GitEnough")
        name.setStyleSheet("font-size: 20pt; font-weight: 800;")
        tagline = QLabel("The last git tracker you'll need.")
        tagline.setStyleSheet(f"color: {C['muted']}; font-size: 11pt;")
        version = QLabel(f"Version {__version__}  ·  <a href='{REPO_URL}' style='color:{C['accent']}'>"
                         f"{REPO_URL.removeprefix('https://')}</a>")
        version.setObjectName("muted")
        version.setTextFormat(Qt.RichText)
        version.setOpenExternalLinks(True)
        titles.addWidget(name)
        titles.addWidget(tagline)
        titles.addWidget(version)
        head.addLayout(titles, 1)
        lay.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(18)
        body.addWidget(GoodEnoughSeal(), 0, Qt.AlignTop)
        quote = QLabel("<p style='font-size:13pt; font-style:italic; margin:0'>"
                       "“It's not perfect.<br>It's good enough.”</p>"
                       f"<p style='color:{C['muted']}; margin-top:6px'>David Goodenough, patron saint of shipping</p>"
                       f"<p style='color:{C['muted']}; margin-top:18px'>Every repository under one root folder: "
                       "status, pull, branches, stashes, diffs, history and blame, without opening each one.</p>")
        quote.setTextFormat(Qt.RichText)
        quote.setWordWrap(True)
        body.addWidget(quote, 1, Qt.AlignVCenter)
        lay.addLayout(body)
        lay.addStretch()
