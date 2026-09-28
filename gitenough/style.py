from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap

C = {
    "bg": "#0e1014",
    "surface": "#15181f",
    "surface2": "#1b1f28",
    "hover": "#20252f",
    "border": "#262b36",
    "text": "#e7e9ee",
    "muted": "#8a91a1",
    "faint": "#5c6373",
    "accent": "#7c8cff",
    "accent_hover": "#909eff",
    "green": "#3ecf8e",
    "orange": "#f5a524",
    "red": "#f26b6b",
    "blue": "#5aa9ff",
    "yellow": "#f2c94c",
    "grey": "#7a8191",
    "violet": "#b392f0",
    "pink": "#f47fb4",
    "op": "#ff9469",
}


def pill_css(color: str) -> str:
    q = QColor(color)
    return (f"background: rgba({q.red()},{q.green()},{q.blue()},0.14);"
            f"color: {color}; border-radius: 11px; padding: 3px 11px; font-weight: 600;")


QSS = f"""
* {{ font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 10pt; color: {C['text']}; }}
QMainWindow, QDialog, #central {{ background: {C['bg']}; }}
QToolTip {{ background: {C['surface2']}; color: {C['text']}; border: 1px solid {C['border']};
            padding: 6px 8px; border-radius: 6px; }}

#title {{ font-size: 17pt; font-weight: 700; }}
#subtitle {{ color: {C['muted']}; }}
#subtitle:hover {{ color: {C['accent']}; }}
#summary {{ color: {C['muted']}; }}
#muted {{ color: {C['muted']}; font-size: 9pt; }}
#emptyTitle {{ font-size: 14pt; font-weight: 600; }}

QPushButton {{ background: {C['surface2']}; border: 1px solid {C['border']}; border-radius: 8px;
               padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ background: {C['hover']}; border-color: #333a48; }}
QPushButton:pressed {{ background: {C['surface']}; }}
QPushButton:disabled {{ color: {C['faint']}; background: {C['surface']}; }}
QPushButton#primary {{ background: {C['accent']}; border: none; color: #0b0d12; }}
QPushButton#primary:hover {{ background: {C['accent_hover']}; }}
QPushButton#primary:disabled {{ background: #3a4270; color: #8a91c0; }}
QPushButton#ghost {{ background: transparent; border: none; color: {C['muted']}; padding: 5px 8px; }}
QPushButton#ghost:hover {{ color: {C['text']}; background: {C['hover']}; }}
QPushButton#rowAction {{ padding: 4px 12px; font-size: 9pt; }}
QPushButton#danger {{ color: {C['red']}; }}
QPushButton#danger:hover {{ background: rgba(242,107,107,0.12); border-color: rgba(242,107,107,0.45); }}
QPushButton#danger:disabled {{ color: {C['faint']}; }}
QPushButton#dangerSmall {{ color: {C['red']}; padding: 4px 12px; font-size: 9pt; }}
QPushButton#dangerSmall:hover {{ background: rgba(242,107,107,0.12); }}
#selBar {{ background: #1a2038; border-bottom: 1px solid #2d3660; }}

QLineEdit, QPlainTextEdit {{ background: {C['surface']}; border: 1px solid {C['border']};
            border-radius: 8px; padding: 6px 10px; selection-background-color: {C['accent']}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {C['accent']}; }}
QPlainTextEdit {{ font-family: "Cascadia Mono", Consolas; font-size: 9.5pt; }}

QComboBox {{ background: {C['surface2']}; border: 1px solid {C['border']}; border-radius: 7px;
             padding: 4px 10px; min-height: 20px; }}
QComboBox:hover {{ border-color: #3a4150; }}
QComboBox:disabled {{ color: {C['faint']}; background: {C['surface']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url(:/arrow.png); width: 10px; height: 6px; margin-right: 10px; }}
QComboBox::down-arrow:disabled {{ image: none; }}
QComboBox QAbstractItemView {{ background: {C['surface2']}; border: 1px solid {C['border']};
             selection-background-color: {C['hover']}; outline: none; padding: 4px; }}

QCheckBox::indicator {{ width: 17px; height: 17px; border-radius: 5px; border: 1.5px solid #3a4150;
                        background: {C['surface']}; }}
QCheckBox::indicator:hover {{ border-color: {C['accent']}; }}
QCheckBox::indicator:checked {{ background: {C['accent']}; border-color: {C['accent']};
                                image: url(:/check.png); }}

QTableWidget {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 12px;
                gridline-color: transparent; outline: none; }}
QTableWidget::item {{ border-bottom: 1px solid {C['border']}; padding: 0; }}
QTableWidget::item:selected {{ background: {C['surface2']}; }}
QHeaderView::section {{ background: {C['surface']}; color: {C['faint']}; border: none;
                        border-bottom: 1px solid {C['border']}; padding: 10px 12px; font-size: 8.5pt;
                        font-weight: 700; text-transform: uppercase; }}
QTableCornerButton::section {{ background: {C['surface']}; border: none; }}

QTabWidget::pane {{ border: 1px solid {C['border']}; border-radius: 10px; background: {C['surface']};
                    top: -1px; }}
QTabBar::tab {{ background: transparent; color: {C['muted']}; padding: 8px 16px; margin-right: 4px;
                border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:selected {{ color: {C['text']}; border-bottom-color: {C['accent']}; }}
QTabBar::tab:hover {{ color: {C['text']}; }}

QProgressBar {{ background: transparent; border: none; max-height: 3px; }}
QProgressBar::chunk {{ background: {C['accent']}; border-radius: 1px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2c3240; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4150; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2c3240; border-radius: 4px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: #3a4150; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ width: 0; background: none; }}

QToolButton {{ background: transparent; border: none; border-radius: 6px; padding: 4px; }}
QToolButton:hover {{ background: {C['hover']}; }}
QToolButton:pressed {{ background: {C['surface']}; }}

QMenu {{ background: {C['surface2']}; border: 1px solid {C['border']}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background: {C['hover']}; }}
QMenu::item:disabled {{ color: {C['faint']}; }}
QMenu::separator {{ height: 1px; background: {C['border']}; margin: 5px 8px; }}

QSplitter::handle {{ background: {C['border']}; }}
#windowHeader {{ background: {C['bg']}; border-bottom: 1px solid {C['border']}; }}
#sidePanel {{ background: {C['bg']}; }}
#sectionTitle {{ font-weight: 700; font-size: 9.5pt; }}
#branchChip {{ background: {C['surface2']}; border: 1px solid {C['border']}; border-radius: 10px;
               padding: 2px 10px; color: {C['muted']}; }}
#commitInfo {{ background: transparent; padding: 2px; }}
QScrollArea#plainScroll {{ border: none; background: transparent; }}
QScrollArea#plainScroll > QWidget > QWidget {{ background: transparent; }}

QListWidget#files {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 10px;
                     padding: 4px 0; outline: none; }}
QListWidget#files::item {{ border: none; background: transparent; }}
QTreeWidget#fileView {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 10px;
                        padding: 4px 0; outline: none; }}
QTreeWidget#fileView::item {{ border: none; background: transparent; }}
QTreeWidget#fileView::branch {{ background: transparent; }}
QTreeWidget#fileView {{ selection-background-color: transparent; show-decoration-selected: 0; }}
QTreeWidget#fileView::item:selected {{ background: transparent; }}
QTreeWidget#fileView::branch:selected {{ background: transparent; }}
QTreeWidget#fileView::branch:has-children:closed {{ image: url(:/branch-closed.png); }}
QTreeWidget#fileView::branch:has-children:open {{ image: url(:/branch-open.png); }}
QTabBar {{ background: {C['bg']}; qproperty-drawBase: 0; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab:!selected {{ background: {C['bg']}; }}
#extBar {{ color: {C['muted']}; font-size: 8.5pt; padding: 0 2px 2px 2px; }}
QPushButton#filterChip {{ background: transparent; border: 1px solid {C['border']}; border-radius: 13px;
                          padding: 3px 12px; font-size: 9pt; font-weight: 600; color: {C['muted']}; }}
QPushButton#filterChip:hover {{ color: {C['text']}; border-color: #3a4150; }}
QPushButton#filterChip:checked {{ background: #2a3152; color: {C['text']}; border-color: #3d4778; }}
QToolButton:checked {{ background: #2a3152; }}
QTreeWidget#branchTree {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 10px;
                          outline: none; padding: 4px; }}
QTreeWidget#branchTree::item {{ height: 28px; border: none; }}
QTreeWidget#branchTree::item:hover {{ background: {C['hover']}; }}
QTreeWidget#branchTree::item:selected {{ background: #262c3a; color: {C['text']}; }}
QSpinBox {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 8px;
            padding: 5px 8px; }}

QTableView#history {{ background: {C['surface']}; border: none; outline: none;
                      selection-background-color: #262c3a; selection-color: {C['text']}; }}
QTableView#history::item {{ padding-left: 8px; border: none; }}
QTableView#history::item:hover {{ background: {C['hover']}; }}
QTableView#history::item:selected {{ background: #262c3a; }}

#diffHeader {{ background: {C['surface']}; border-bottom: 1px solid {C['border']}; }}
#diffNotes {{ background: #141824; color: {C['muted']}; padding: 6px 14px; border-bottom: 1px solid {C['border']}; }}
QPlainTextEdit#diff {{ background: #0f1116; border: none; border-radius: 0; padding: 0;
                       selection-background-color: #34406b; }}
QPushButton#segL, QPushButton#segR {{ padding: 4px 12px; font-size: 9pt; border-radius: 0; }}
QPushButton#segL {{ border-top-left-radius: 7px; border-bottom-left-radius: 7px; }}
QPushButton#segR {{ border-top-right-radius: 7px; border-bottom-right-radius: 7px; border-left: none; }}
QPushButton#segL:checked, QPushButton#segR:checked {{ background: #2a3152; color: {C['text']}; border-color: #3d4778; }}

QStatusBar {{ background: {C['bg']}; color: {C['muted']}; padding: 0 18px 4px 18px; }}
"""


def app_icon(size: int = 256) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor("#8f9bff"))
    grad.setColorAt(1, QColor("#5b6cf0"))
    p.setBrush(grad)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    s = size / 100
    pen = QPen(QColor("#0e1014"), 7 * s, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(35 * s, 28 * s), QPointF(35 * s, 72 * s))
    p.drawArc(QRectF(35 * s, 30 * s, 32 * s, 30 * s), 0, -90 * 16)
    p.setBrush(QColor("#0e1014"))
    p.setPen(Qt.NoPen)
    for cx, cy in ((35, 26), (35, 74), (67, 34)):
        p.drawEllipse(QPointF(cx * s, cy * s), 9 * s, 9 * s)
    p.setBrush(QColor("#e7e9ee"))
    for cx, cy in ((35, 26), (35, 74), (67, 34)):
        p.drawEllipse(QPointF(cx * s, cy * s), 4.5 * s, 4.5 * s)
    p.end()
    return pm


def check_pixmap() -> QPixmap:
    pm = QPixmap(34, 34)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor("#0b0d12"), 4.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline([QPointF(9, 17.5), QPointF(14.5, 23), QPointF(25, 11)])
    p.end()
    return pm


def chevron_pixmap(direction: str) -> QPixmap:
    """Small chevron used for tree folders ("right" collapsed, "down" expanded)."""
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(C["muted"]), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    pts = [(6, 4), (10, 8), (6, 12)] if direction == "right" else [(4, 6), (8, 10), (12, 6)]
    p.drawPolyline([QPointF(x, y) for x, y in pts])
    p.end()
    return pm


def arrow_pixmap() -> QPixmap:
    pm = QPixmap(20, 12)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(C["muted"]), 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline([QPointF(4, 3), QPointF(10, 9), QPointF(16, 3)])
    p.end()
    return pm


def make_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_icon(size))
    return icon
