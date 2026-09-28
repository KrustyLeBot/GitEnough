"""Merge tool: resolve every conflict block of a file with one side, both, none or your own text."""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from . import conflicts as cf
from .diff_view import DiffEditor, Line, lexer_for, mono_font
from .git_ops import run_git
from .style import C
from .widgets import ElidedLabel

LEFT_COLOR, RIGHT_COLOR = "#5aa9ff", "#b392f0"
CHOICE_TEXT = {cf.LEFT: "{l}", cf.RIGHT: "{r}", cf.LEFT_RIGHT: "{l}, then {r}", cf.RIGHT_LEFT: "{r}, then {l}",
               cf.NONE: "neither side", cf.CUSTOM: "your own text"}


def _code_box(lines: list[str], lexer, tint: str) -> DiffEditor:
    ed = DiffEditor("new")
    ed.set_lines([Line("ctx", None, i + 1, t) for i, t in enumerate(lines)]
                 or [Line("meta", None, None, "(empty: this side removed the lines)")], lexer)
    ed.setStyleSheet(f"QPlainTextEdit#diff {{ background: {tint}; }}")
    height = ed.fontMetrics().lineSpacing() * min(max(len(lines), 2), 14) + 14
    ed.setFixedHeight(height)
    return ed


class BlockCard(QFrame):
    changed = Signal()

    def __init__(self, index: int, total: int, block: cf.Block, before: list[str], left: str, right: str, lexer):
        super().__init__()
        self.block, self.left_label, self.right_label = block, left, right
        self._updating = False
        self.setObjectName("conflictCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(f"Conflict {index} of {total}")
        title.setStyleSheet("font-weight: 700;")
        self.state = QLabel("")
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.state)
        lay.addLayout(head)
        if before:
            ctx = QLabel("\n".join(before[-3:]))
            ctx.setFont(mono_font())
            ctx.setStyleSheet(f"color: {C['faint']};")
            lay.addWidget(ctx)

        sides = QHBoxLayout()
        sides.setSpacing(10)
        for label, lines, color, choice in ((left, block.left, LEFT_COLOR, cf.LEFT),
                                            (right, block.right, RIGHT_COLOR, cf.RIGHT)):
            col = QVBoxLayout()
            col.setSpacing(4)
            name = ElidedLabel(f"●  {label}", Qt.ElideMiddle)
            name.setStyleSheet(f"color: {color}; font-weight: 700;")
            col.addWidget(name)
            col.addWidget(_code_box(lines, lexer, "#121826" if choice == cf.LEFT else "#1a1628"))
            use = QPushButton(f"Use {label}")
            use.setObjectName("rowAction")
            use.clicked.connect(lambda _=False, c=choice: self.choose(c))
            col.addWidget(use)
            sides.addLayout(col, 1)
        lay.addLayout(sides)

        more = QHBoxLayout()
        more.setSpacing(6)
        for choice, text in ((cf.LEFT_RIGHT, f"{left}, then {right}"), (cf.RIGHT_LEFT, f"{right}, then {left}"),
                             (cf.NONE, "Neither")):
            b = QPushButton(text)
            b.setObjectName("rowAction")
            b.clicked.connect(lambda _=False, c=choice: self.choose(c))
            more.addWidget(b)
        more.addStretch()
        lay.addLayout(more)

        lay.addWidget(QLabel("Result  (edit it freely)"))
        self.result = QPlainTextEdit()
        self.result.setFont(mono_font())
        self.result.setPlaceholderText("Pick a side above, or type the resolved code here")
        self.result.textChanged.connect(self._edited)
        lay.addWidget(self.result)
        self.refresh()

    def choose(self, choice: str):
        self.block.choice = choice
        self._updating = True
        self.result.setPlainText("\n".join(self.block.result()))
        self._updating = False
        self.refresh()

    def _edited(self):
        if self._updating:
            return
        text = self.result.toPlainText()
        self.block.custom = text.split("\n") if text else []
        self.block.choice = cf.CUSTOM
        self.refresh()

    def refresh(self):
        b = self.block
        lines = max(len(b.result()), 2)
        self.result.setFixedHeight(self.result.fontMetrics().lineSpacing() * min(lines, 12) + 16)
        if b.choice:
            what = CHOICE_TEXT[b.choice].format(l=self.left_label, r=self.right_label)
            self.state.setText(f"✓  {what}")
            self.state.setStyleSheet(f"color: {C['green']}; font-weight: 600;")
        else:
            self.state.setText("Unresolved")
            self.state.setStyleSheet(f"color: {C['orange']}; font-weight: 600;")
        self.changed.emit()


class MergeToolWindow(QWidget):
    """One conflicted file. left = git's "ours", right = git's "theirs", both shown with their branch names."""

    resolved = Signal(str)

    def __init__(self, main, project, file: str, left: str, right: str):
        super().__init__(main, Qt.Window)
        self.main, self.p, self.file = main, project, file
        self.left, self.right = left, right
        self.full_path = os.path.join(project.path, file)
        with open(self.full_path, "rb") as fh:
            self.cf = cf.parse(fh.read())
        self.cards: list[BlockCard] = []
        self.setWindowTitle(f"Resolve {file} · {project.title or project.name}")
        self.resize(1300, 860)

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
        for label, color in ((left, LEFT_COLOR), (right, RIGHT_COLOR)):
            chip = QLabel(f"●  {label}")
            chip.setStyleSheet(f"color: {color}; font-weight: 600;")
            hl.addWidget(chip)
        root.addWidget(header)

        bar = QWidget()
        bar.setObjectName("sidePanel")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 6)
        all_left = QPushButton(f"All conflicts: {left}")
        all_left.clicked.connect(lambda: self.choose_all(cf.LEFT))
        all_right = QPushButton(f"All conflicts: {right}")
        all_right.clicked.connect(lambda: self.choose_all(cf.RIGHT))
        bl.addWidget(all_left)
        bl.addWidget(all_right)
        bl.addStretch()
        open_code = QPushButton("Open in VS Code")
        open_code.clicked.connect(lambda: main.open_code(self.full_path))
        bl.addWidget(open_code)
        root.addWidget(bar)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("mergeScroll")
        holder = QWidget()
        holder.setObjectName("sidePanel")
        cards = QVBoxLayout(holder)
        cards.setContentsMargins(16, 12, 16, 16)
        cards.setSpacing(12)
        lexer = lexer_for(os.path.basename(file))
        conflicts = self.cf.conflicts
        before: list[str] = []
        for block in self.cf.blocks:
            if not block.conflict:
                before = block.lines
                continue
            card = BlockCard(len(self.cards) + 1, len(conflicts), block, before, left, right, lexer)
            card.changed.connect(self.update_state)
            self.cards.append(card)
            cards.addWidget(card)
        cards.addStretch()
        scroll.setWidget(holder)
        self.tabs.addTab(scroll, f"Conflicts ({len(conflicts)})")

        whole = QWidget()
        whole.setObjectName("sidePanel")
        wl = QVBoxLayout(whole)
        wl.setContentsMargins(16, 12, 16, 16)
        self.manual = QCheckBox("Edit the whole file by hand (the text below is what gets saved)")
        self.manual.toggled.connect(self.toggle_manual)
        wl.addWidget(self.manual)
        self.whole = QPlainTextEdit()
        self.whole.setFont(mono_font())
        self.whole.setReadOnly(True)
        wl.addWidget(self.whole, 1)
        self.tabs.addTab(whole, "Whole file")
        self.tabs.currentChanged.connect(lambda i: i == 1 and not self.manual.isChecked() and self.show_whole())
        root.addWidget(self.tabs, 1)

        foot = QWidget()
        foot.setObjectName("windowHeader")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 10, 16, 10)
        self.status = QLabel("")
        fl.addWidget(self.status, 1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        self.save_btn = QPushButton("Save && mark resolved")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        fl.addWidget(cancel)
        fl.addWidget(self.save_btn)
        root.addWidget(foot)
        QShortcut(QKeySequence("Ctrl+S"), self, self.save)
        self.update_state()

    def choose_all(self, choice: str):
        for card in self.cards:
            card.choose(choice)

    def show_whole(self):
        self.whole.setPlainText(self.cf.compose().replace("\r\n", "\n"))

    def toggle_manual(self, on: bool):
        if on:
            self.show_whole()
        self.whole.setReadOnly(not on)
        self.update_state()

    def update_state(self):
        if self.manual.isChecked():
            self.status.setText("Saving the text of the Whole file tab as typed")
            self.save_btn.setEnabled(True)
            return
        left = self.cf.unresolved()
        total = len(self.cf.conflicts)
        self.status.setText(f"{total - left} of {total} conflicts resolved" if left else
                            ("The conflict is resolved" if total == 1 else f"All {total} conflicts resolved"))
        self.status.setStyleSheet(f"color: {C['orange'] if left else C['green']};")
        self.save_btn.setEnabled(left == 0)

    def save(self):
        if not self.save_btn.isEnabled():
            return
        if self.manual.isChecked():
            text = self.whole.toPlainText().replace("\n", self.cf.newline)
        else:
            text = self.cf.compose()
        if any(cf._marker(l, "<") or cf._marker(l, ">") for l in text.splitlines()):
            if QMessageBox.question(self, "Conflict markers left",
                                    "The result still contains conflict markers (<<<<<<< / >>>>>>>).\n\n"
                                    "Save it anyway?") != QMessageBox.Yes:
                return
        with open(self.full_path, "wb") as fh:
            fh.write(cf.encode(self.cf, text))
        run_git(["add", "--", self.file], cwd=self.p.path, timeout=30)
        self.resolved.emit(self.file)
        self.close()
