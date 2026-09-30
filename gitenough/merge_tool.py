"""Merge tool: resolve every conflict block of a file with one side, both, none or your own text."""

import os
import subprocess
import tempfile
from difflib import SequenceMatcher

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from . import conflicts as cf
from .diff_view import DiffEditor, Line, lexer_for, mono_font
from .git_ops import GitError, run_git, run_git_bytes
from .style import C
from .widgets import ElidedLabel, keep_size

LEFT_COLOR, RIGHT_COLOR = "#5aa9ff", "#b392f0"
CHOICE_TEXT = {cf.LEFT: "{l}", cf.RIGHT: "{r}", cf.LEFT_RIGHT: "{l}, then {r}", cf.RIGHT_LEFT: "{r}, then {l}",
               cf.NONE: "neither side", cf.CUSTOM: "your own text"}


def _code_box(lines: list[str], lexer, tint: str, first_no: int = 1, marked: set[int] | None = None,
              mark_color: str = "", empty_text: str = "", max_lines: int = 14) -> DiffEditor:
    """Read-only code with line numbers; `marked` lines get a tint of the side's colour."""
    ed = DiffEditor("new")
    if lines:
        ed.set_lines([Line("ctx", None, first_no + i, t) for i, t in enumerate(lines)], lexer)
    else:
        ed.set_lines([Line("meta", None, None, empty_text)], None)
    ed.setStyleSheet(f"QPlainTextEdit#diff {{ background: {tint}; }}")
    if marked and mark_color:
        tint_color = QColor(mark_color)
        tint_color.setAlpha(46)
        block = ed.document().firstBlock()
        n = 0
        while block.isValid():
            if n in marked:
                fmt = QTextBlockFormat()
                fmt.setBackground(tint_color)
                QTextCursor(block).setBlockFormat(fmt)
            block = block.next()
            n += 1
    height = ed.fontMetrics().lineSpacing() * min(max(len(lines), 1), max_lines) + 14
    ed.setFixedHeight(height)
    return ed


def describe(side: list[str], base: list[str] | None) -> str:
    """What a side did to the common ancestor's lines, in plain words."""
    if base is None:
        return ""
    if side == base:
        return "unchanged"
    if not side:
        return f"deleted {len(base)} line{'s' if len(base) != 1 else ''}"
    if not base:
        return f"added {len(side)} line{'s' if len(side) != 1 else ''}"
    return f"changed {len(base)} → {len(side)} line{'s' if len(side) != 1 else ''}"


def empty_text(lines: list[str]) -> str:
    return "(nothing: this side deleted these lines)" if not lines else ""


def blank_note(lines: list[str]) -> str:
    """A side made only of blank lines looks empty: say it."""
    if lines and not any(line.strip() for line in lines):
        return f"{len(lines)} blank line{'s' if len(lines) != 1 else ''}"
    return ""


def differing(a: list[str], b: list[str]) -> tuple[set[int], set[int]]:
    """Indexes of the lines of a and of b that the other side does not have."""
    sm = SequenceMatcher(None, [x.strip() for x in a], [x.strip() for x in b], autojunk=False)
    only_a, only_b = set(range(len(a))), set(range(len(b)))
    for block in sm.get_matching_blocks():
        only_a -= set(range(block.a, block.a + block.size))
        only_b -= set(range(block.b, block.b + block.size))
    return only_a, only_b


class BlockCard(QFrame):
    changed = Signal()

    def __init__(self, index: int, total: int, block: cf.Block, before: list[str], left: str, right: str, lexer,
                 after: list[str] | None = None, line_no: int = 1, base: list[str] | None = None):
        super().__init__()
        self.block, self.left_label, self.right_label = block, left, right
        self._updating = False
        self.setObjectName("conflictCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(f"Conflict {index} of {total}  <span style='color:{C['faint']}; font-weight:400'>"
                       f"· line {line_no}</span>")
        title.setTextFormat(Qt.RichText)
        title.setStyleSheet("font-weight: 700;")
        self.state = QLabel("")
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.state)
        lay.addLayout(head)
        before = before[-3:] if before else []
        if before:
            lay.addWidget(_code_box(before, lexer, "#0f1116", first_no=line_no - len(before), max_lines=3))

        only_left, only_right = differing(block.left, block.right)
        sides = QHBoxLayout()
        sides.setSpacing(10)
        for label, lines, color, choice, marked in ((left, block.left, LEFT_COLOR, cf.LEFT, only_left),
                                                    (right, block.right, RIGHT_COLOR, cf.RIGHT, only_right)):
            col = QVBoxLayout()
            col.setSpacing(4)
            name_row = QHBoxLayout()
            name = ElidedLabel(f"●  {label}", Qt.ElideMiddle)
            name.setStyleSheet(f"color: {color}; font-weight: 700;")
            what = ", ".join(x for x in (describe(lines, base), blank_note(lines)) if x)
            info = QLabel(what)
            info.setObjectName("muted")
            info.setToolTip("Compared with the common ancestor, the version before both branches changed it")
            name_row.addWidget(name, 1)
            name_row.addWidget(info)
            col.addLayout(name_row)
            col.addWidget(_code_box(lines, lexer, "#121826" if choice == cf.LEFT else "#1a1628", marked=marked,
                                    mark_color=color, empty_text=empty_text(lines)))
            use = QPushButton(f"Use {label}")
            use.setObjectName("rowAction")
            use.clicked.connect(lambda _=False, c=choice: self.choose(c))
            col.addWidget(use)
            sides.addLayout(col, 1)
        lay.addLayout(sides)
        if base is not None:
            self.base_btn = QPushButton("Show common ancestor")
            self.base_btn.setObjectName("rowAction")
            self.base_btn.setCheckable(True)
            self.base_box = _code_box(base, lexer, "#15171c", empty_text="(these lines did not exist yet)")
            self.base_box.hide()
            self.base_btn.toggled.connect(lambda on: (self.base_box.setVisible(on), self.base_btn.setText(
                "Hide common ancestor" if on else "Show common ancestor")))
            base_row = QHBoxLayout()
            base_row.addWidget(self.base_btn)
            hint = QLabel("the version before both branches changed it")
            hint.setObjectName("muted")
            base_row.addWidget(hint)
            base_row.addStretch()
            lay.addLayout(base_row)
            lay.addWidget(self.base_box)

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

        result_row = QHBoxLayout()
        result_row.addWidget(QLabel("Result  (edit it freely)"))
        self.result_note = QLabel("")
        self.result_note.setObjectName("muted")
        result_row.addWidget(self.result_note)
        result_row.addStretch()
        lay.addLayout(result_row)
        self.result = QPlainTextEdit()
        self.result.setFont(mono_font())
        self.result.setPlaceholderText("Pick a side above, or type the resolved code here")
        self.result.textChanged.connect(self._edited)
        lay.addWidget(self.result)
        if after:
            lay.addWidget(_code_box(after[:3], lexer, "#0f1116",
                                    first_no=line_no + max(len(block.left), len(block.right)), max_lines=3))
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
            self.state.setText(f"✓  Resolved with {what}")
            self.state.setStyleSheet(f"color: {C['green']}; font-weight: 600;")
        else:
            self.state.setText("Unresolved")
            self.state.setStyleSheet(f"color: {C['orange']}; font-weight: 600;")
        result = b.result()
        self.result.setPlaceholderText("(empty result: the conflict lines are removed)" if b.choice else
                                       "Pick a side above, or type the resolved code here")
        self.result_note.setText("" if not b.choice else
                                 "· the conflict lines are removed" if not result else
                                 "· only blank lines" if not any(x.strip() for x in result) else "")
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
        self._fill_base()
        self.cards: list[BlockCard] = []
        self.setWindowTitle(f"Resolve {file} · {project.title or project.name}")
        self.resize(1300, 860)
        keep_size(self, self.main.config, "merge_tool")

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
        line_no = 1  # where each conflict starts in the result, counting the left side
        blocks = self.cf.blocks
        for i, block in enumerate(blocks):
            if not block.conflict:
                before = block.lines
                line_no += len(block.lines)
                continue
            after = blocks[i + 1].lines if i + 1 < len(blocks) and not blocks[i + 1].conflict else []
            base = block.base if block.base or self.has_base else None
            card = BlockCard(len(self.cards) + 1, len(conflicts), block, before, left, right, lexer, after, line_no,
                             base)
            line_no += len(block.left)
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

    def _fill_base(self):
        """The common ancestor of each conflict, from git's index (stages 1-3), when the markers lack it.

        Rebuilt in memory with git merge-file --diff3; used only when it yields the same conflicts.
        """
        self.has_base = any(b.base for b in self.cf.conflicts)
        if self.has_base:
            return
        try:
            stages = [run_git_bytes(["show", f":{n}:{self.file}"], cwd=self.p.path, timeout=30) for n in (1, 2, 3)]
        except GitError:
            return  # no ancestor (both added the file) or not an index conflict
        folder = tempfile.mkdtemp(prefix="gitenough-merge-")
        try:
            paths = []
            for name, data in zip(("base", "ours", "theirs"), stages):
                path = os.path.join(folder, name)
                with open(path, "wb") as fh:
                    fh.write(data)
                paths.append(path)
            out = subprocess.run(["git", "merge-file", "-p", "--diff3", paths[1], paths[0], paths[2]],
                                 capture_output=True, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.SubprocessError):
            return
        finally:
            for name in os.listdir(folder):
                os.remove(os.path.join(folder, name))
            os.rmdir(folder)
        rebuilt = cf.parse(out)

        def version(parsed: cf.ConflictFile, side: str) -> list[str]:
            return [x for b in parsed.blocks for x in (getattr(b, side) if b.conflict else b.lines)]

        # git merge joins nearby conflicts into one; merge-file --diff3 keeps them apart, each with its
        # ancestor. When both describe exactly the same two versions, the finer split is shown.
        if version(rebuilt, "left") == version(self.cf, "left") and \
                version(rebuilt, "right") == version(self.cf, "right"):
            self.cf.blocks = rebuilt.blocks
            self.has_base = True

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
