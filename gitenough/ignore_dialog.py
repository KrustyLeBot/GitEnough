"""Choose how to ignore files: this file, its extension, a folder, or a custom pattern."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QRadioButton, QVBoxLayout)

from .file_view import extension
from .repo import FileChange, ignore_pattern
from .style import C


def _options(files: list[FileChange]) -> list[tuple[str, list[str]]]:
    """(label, patterns) choices, most specific first."""
    paths = [f.path for f in files]
    opts = []
    if len(paths) == 1:
        opts.append((f"This file only: {paths[0]}", [ignore_pattern(paths[0])]))
    else:
        opts.append((f"These {len(paths)} files", [ignore_pattern(p) for p in paths]))
    exts = sorted({extension(p) for p in paths} - {"(none)"})
    if exts:
        pats = [f"*{e}" for e in exts]
        opts.append((f"Every {', '.join(pats)} file in the repository", pats))
        folders = {p.rsplit("/", 1)[0] for p in paths if "/" in p}
        if len(folders) == 1:
            folder = folders.pop()
            opts.append((f"Every {', '.join(pats)} file in {folder}/",
                         [ignore_pattern(folder, True).rstrip("/") + "/" + p for p in pats]))
    # Common parent folders, deepest first.
    parts = [p.split("/")[:-1] for p in paths]
    common = []
    for level in zip(*parts):
        if len(set(level)) != 1:
            break
        common.append(level[0])
    for depth in range(len(common), 0, -1):
        folder = "/".join(common[:depth])
        opts.append((f"The folder {folder}/", [ignore_pattern(folder, True)]))
    if common:
        name = common[-1]
        opts.append((f"Every folder named {name}/ anywhere (e.g. bin/, obj/)", [f"{name}/"]))
    return opts


class IgnoreDialog(QDialog):
    def __init__(self, parent, files: list[FileChange]):
        super().__init__(parent)
        self.files = files
        self.setWindowTitle("Ignore files")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        lay.setSpacing(8)
        title = QLabel("What should git ignore?")
        title.setStyleSheet("font-size: 12pt; font-weight: 700;")
        lay.addWidget(title)

        self.group = QButtonGroup(self)
        self.choices: list[list[str]] = []
        for i, (label, patterns) in enumerate(_options(files)):
            radio = QRadioButton(label)
            radio.setToolTip("\n".join(patterns))
            self.group.addButton(radio, i)
            self.choices.append(patterns)
            lay.addWidget(radio)
        custom = QRadioButton("Custom pattern")
        self.group.addButton(custom, len(self.choices))
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("e.g. *.log, /build/, secrets.json")
        self.custom.textChanged.connect(lambda: (custom.setChecked(True), self.update_preview()))
        lay.addWidget(custom)
        lay.addWidget(self.custom)
        self.group.button(0).setChecked(True)
        self.group.idToggled.connect(lambda *_: self.update_preview())

        lay.addSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Write to"))
        self.target = QComboBox()
        self.target.addItem(".gitignore  (committed, shared with the team)", False)
        self.target.addItem(".git/info/exclude  (this machine only)", True)
        row.addWidget(self.target, 1)
        lay.addLayout(row)

        tracked = [f for f in files if f.code != "?"]
        self.untrack = QCheckBox(f"Stop tracking {len(tracked)} already committed file(s) (git rm --cached, "
                                 "the files stay on disk)")
        self.untrack.setChecked(True)
        self.untrack.setVisible(bool(tracked))
        lay.addWidget(self.untrack)

        self.preview = QLabel("")
        self.preview.setTextFormat(Qt.RichText)
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(f"background: {C['surface2']}; border: 1px solid {C['border']};"
                                   "border-radius: 8px; padding: 8px 10px; font-family: Consolas;")
        lay.addWidget(self.preview)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Ignore")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(lambda: self.patterns() and self.accept())
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        lay.addLayout(buttons)
        self.update_preview()

    def patterns(self) -> list[str]:
        idx = self.group.checkedId()
        if idx < len(self.choices):
            return self.choices[idx]
        return [p.strip() for p in self.custom.text().split(",") if p.strip()]

    def local_only(self) -> bool:
        return bool(self.target.currentData())

    def untracked_paths(self) -> list[str]:
        return [f.path for f in self.files if f.code != "?"] if self.untrack.isChecked() else []

    def update_preview(self):
        lines = self.patterns()
        esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
        self.preview.setText("Lines added:<br>" + "<br>".join(f"<b>{esc(l)}</b>" for l in lines)
                             if lines else "Enter a pattern")
