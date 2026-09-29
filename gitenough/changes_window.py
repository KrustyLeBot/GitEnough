import os
import subprocess
import time

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget)

from . import rebase, repo
from .diff_view import DiffView, parse_diff
from .errors import explain
from .file_view import ExtensionBar, FileView, extension
from .ignore_dialog import IgnoreDialog
from .repo import FileChange
from .style import C
from .widgets import ElidedLabel, ErrorDialog, icon_button, keep_size


def _load_diff(path: str, fc: FileChange, ws: bool, full: bool):
    text = repo.change_diff(path, fc, ws, full)
    data = parse_diff(text)
    data.signature = hash(text)  # lets a refresh skip re-rendering an unchanged diff
    return data


def _content_search(path: str, files: list[FileChange], term: str) -> set:
    term = term.lower()
    hits = set()
    for key, text in repo.all_diffs(path, files).items():
        # Only look at added/removed lines, not at the diff headers.
        if any(term in line[1:].lower() for line in text.splitlines() if line[:1] in "+-"
               and not line.startswith(("+++", "---"))):
            hits.add(key)
    return hits


def tree_toggle(main, views: list[FileView]):
    """Header button switching every file view of a window between list and folder tree."""
    btn = icon_button("tree_view", "Show files as a folder tree")
    btn.setCheckable(True)
    btn.setChecked(main.config.file_tree)

    def toggled(on: bool):
        main.config.file_tree = on
        main.config.save()
        for v in views:
            v.set_tree_mode(on)

    btn.toggled.connect(toggled)
    return btn


class StashFilesDialog(QDialog):
    """Asks for a message and whether the stashed files also stay in the working folder."""

    def __init__(self, parent, files: list[FileChange]):
        super().__init__(parent)
        self.keep = False
        paths = sorted({f.path for f in files}, key=str.lower)
        new = len({f.path for f in files if f.code in "?A"})
        self.setWindowTitle("Stash files")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(8)
        head = QLabel(f"Stash {len(paths)} file{'s' if len(paths) != 1 else ''}")
        head.setStyleSheet("font-size: 12pt; font-weight: 700;")
        lay.addWidget(head)
        names = QLabel("\n".join(paths[:12]) + (f"\n… and {len(paths) - 12} more" if len(paths) > 12 else ""))
        names.setObjectName("muted")
        names.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(names)
        note = QLabel("Everything in these files goes into the stash: staged and unstaged changes"
                      + (f", and the {new} new file{'s' if new != 1 else ''}" if new else "")
                      + ". Other files are not touched. Applying the stash later brings them back, "
                        "staged parts staged again.")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addSpacing(4)
        lay.addWidget(QLabel("Message (optional)"))
        self.message = QLineEdit()
        self.message.setPlaceholderText("e.g. WIP login form")
        lay.addWidget(self.message)
        lay.addSpacing(6)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        keep = QPushButton("Stash, keep my changes")
        keep.setToolTip("Save a copy in a stash and leave these files as they are")
        keep.clicked.connect(lambda: self._done(True))
        revert = QPushButton("Stash and revert files")
        revert.setObjectName("primary")
        revert.setDefault(True)
        revert.setToolTip("Move the changes into a stash: these files go back to the last commit"
                          + (", new files are removed" if new else ""))
        revert.clicked.connect(lambda: self._done(False))
        for b in (cancel, keep, revert):
            row.addWidget(b)
        lay.addLayout(row)

    def _done(self, keep: bool):
        self.keep = keep
        self.accept()


class ChangesWindow(QWidget):
    """Staged / unstaged files of one repository, with diff, stage, discard, ignore and commit."""

    changed = Signal()

    def __init__(self, main, project):
        super().__init__(main, Qt.Window)
        self.main, self.p, self.tasks = main, project, main.tasks
        self.path = project.path
        self.staged: list[FileChange] = []
        self.unstaged: list[FileChange] = []
        self.current: FileChange | None = None
        self._shown_key = None
        self.diff_cache: dict = {}
        self.content_hits: set | None = None
        self.search_gen = 0
        self.ext = ""  # extension filter from the extension bars
        self.checked: set[str] = set()  # paths ticked for a stash, in either list
        self.busy = False
        self.last_refresh = 0.0

        self.setWindowTitle(f"Changes · {project.name}")
        self.resize(1440, 860)
        keep_size(self, self.main.config, "changes")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 14, 16, 14)
        title = QLabel(project.name)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        self.branch = QLabel("")
        self.branch.setObjectName("branchChip")
        hl.addWidget(title)
        hl.addWidget(self.branch)
        self.status = ElidedLabel("", Qt.ElideRight)
        self.status.setObjectName("muted")
        self.status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hl.addWidget(self.status, 1)
        compare = QPushButton("Compare with base…")
        compare.setToolTip("Everything this branch changes compared with the base branch (merge request view)")
        compare.clicked.connect(lambda: self.main.open_compare(self.p))
        discard_all = QPushButton("Discard all…")
        discard_all.setObjectName("danger")
        discard_all.setToolTip("Throw away every staged, unstaged and untracked change (or stash them)")
        discard_all.clicked.connect(lambda: self.main.discard_all(self.p, self))
        hl.addWidget(compare)
        hl.addWidget(discard_all)
        hl.addSpacing(6)
        refresh = icon_button("refresh", "Refresh (F5)")
        refresh.clicked.connect(self.refresh)
        bash = icon_button("terminal", "Open Git Bash here")
        bash.clicked.connect(lambda: self.main.open_bash(self.path))
        folder = icon_button("folder", "Open folder")
        folder.clicked.connect(lambda: os.startfile(self.path))
        hl.addWidget(refresh)
        hl.addWidget(bash)
        hl.addWidget(folder)
        root.addWidget(header)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(1)
        split.addWidget(self._left_panel())
        self.diff = DiffView()
        self.diff.options_changed.connect(lambda: self.current and self.show_diff(self.current))
        self.diff.patch_requested.connect(self.apply_patch)
        split.addWidget(self.diff)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 1020])
        root.addWidget(split, 1)

        QShortcut(QKeySequence("F5"), self, self.refresh)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.search.setFocus())
        QShortcut(QKeySequence("Ctrl+Return"), self, lambda: self.do_commit(False))
        self.search_timer = QTimer(self, singleShot=True, interval=300)
        self.search_timer.timeout.connect(self.start_content_search)
        self.refresh()

    # ---------- layout ----------
    def _left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("sidePanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)

        self.staged_view = self._make_view()
        self.unstaged_view = self._make_view()

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search files  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.on_search)
        self.in_content = QCheckBox("In content")
        self.in_content.setToolTip("Also match files whose added or removed lines contain the text")
        self.in_content.toggled.connect(self.on_search)
        row.addWidget(self.search, 1)
        row.addWidget(self.in_content)
        row.addWidget(tree_toggle(self.main, [self.staged_view, self.unstaged_view]))
        lay.addLayout(row)

        self.staged_title, staged_head = self._section("Staged")
        self.staged_all = self._check_all(self.staged_view, staged_head)
        unstage_sel = QPushButton("Unstage")
        unstage_sel.setToolTip("Unstage selected files or folders (Space)")
        unstage_sel.clicked.connect(lambda: self.unstage(self.staged_view.selected_files()))
        unstage_all = QPushButton("Unstage all")
        unstage_all.clicked.connect(lambda: self.unstage(self.staged))
        for b in (unstage_sel, unstage_all):
            b.setObjectName("rowAction")
            staged_head.addWidget(b)
        lay.addLayout(staged_head)
        self.staged_ext = ExtensionBar()
        self.staged_ext.toggled.connect(self.set_ext)
        lay.addWidget(self.staged_ext)
        lay.addWidget(self.staged_view, 2)

        lay.addSpacing(4)
        self.unstaged_title, unstaged_head = self._section("Changes")
        self.unstaged_all = self._check_all(self.unstaged_view, unstaged_head)
        stage_sel = QPushButton("Stage")
        stage_sel.setToolTip("Stage selected files or folders (Space)")
        stage_sel.clicked.connect(lambda: self.stage(self.unstaged_view.selected_files()))
        stage_all = QPushButton("Stage all")
        stage_all.clicked.connect(lambda: self.stage(self.unstaged))
        discard = icon_button("discard", "Discard changes in selected files (Del)")
        discard.clicked.connect(lambda: self.discard(self.unstaged_view.selected_files()))
        for b in (stage_sel, stage_all):
            b.setObjectName("rowAction")
            unstaged_head.addWidget(b)
        unstaged_head.addWidget(discard)
        lay.addLayout(unstaged_head)
        self.unstaged_ext = ExtensionBar()
        self.unstaged_ext.toggled.connect(self.set_ext)
        lay.addWidget(self.unstaged_ext)
        lay.addWidget(self.unstaged_view, 3)

        hint = QLabel("Double-click or Space to stage / unstage · right-click to ignore · tick to stash")
        hint.setObjectName("muted")
        lay.addWidget(hint)

        self.stash_bar = QWidget()
        bar = QHBoxLayout(self.stash_bar)
        bar.setContentsMargins(0, 2, 0, 2)
        self.stash_count = QLabel("")
        clear = QPushButton("Clear")
        clear.setObjectName("rowAction")
        clear.setToolTip("Untick every file")
        clear.clicked.connect(lambda: self.set_checked(list(self.checked), False))
        self.stash_btn = QPushButton("Stash…")
        self.stash_btn.setObjectName("primary")
        self.stash_btn.clicked.connect(self.stash_checked)
        bar.addWidget(self.stash_count, 1)
        bar.addWidget(clear)
        bar.addWidget(self.stash_btn)
        self.stash_bar.hide()
        lay.addWidget(self.stash_bar)

        self.message = QPlainTextEdit()
        self.message.setObjectName("commitMessage")
        self.message.setPlaceholderText("Commit message  (Ctrl+Enter to commit)")
        self.message.setFixedHeight(84)
        self.message.textChanged.connect(self.update_buttons)
        lay.addWidget(self.message)
        buttons = QHBoxLayout()
        self.commit_btn = QPushButton("Commit")
        self.commit_btn.setObjectName("primary")
        self.commit_btn.clicked.connect(lambda: self.do_commit(False))
        self.commit_push_btn = QPushButton("Commit && push")
        self.commit_push_btn.clicked.connect(lambda: self.do_commit(True))
        buttons.addWidget(self.commit_btn, 1)
        buttons.addWidget(self.commit_push_btn, 1)
        lay.addLayout(buttons)
        return panel

    @staticmethod
    def _section(name: str):
        head = QHBoxLayout()
        head.setSpacing(6)
        label = QLabel(name)
        label.setObjectName("sectionTitle")
        label.setTextFormat(Qt.RichText)
        head.addWidget(label)
        head.addStretch()
        return label, head

    def _check_all(self, view: FileView, head: QHBoxLayout) -> QCheckBox:
        box = QCheckBox()
        box.setToolTip("Tick every file of this list (for a stash)")
        box.setFocusPolicy(Qt.NoFocus)
        # Partly ticked: a click ticks the rest; fully ticked: it unticks all.
        box.clicked.connect(lambda _on, v=view: self.set_checked([f.path for f in v.files],
                                                                   not self._all_checked(v)))
        head.insertWidget(0, box)
        return box

    def _all_checked(self, view: FileView) -> bool:
        return bool(view.files) and all(f.path in self.checked for f in view.files)

    def _make_view(self) -> FileView:
        view = FileView(self.main.config.file_tree)
        view.checks = self.checked
        view.check_toggled.connect(lambda files, on: self.set_checked([f.path for f in files], on))
        view.selection_changed.connect(lambda v=view: self.on_selection(v))
        view.activated.connect(lambda files, v=view: self.toggle(v, files))
        view.delete_pressed.connect(lambda files, v=view: v is self.unstaged_view and self.discard(files))
        view.setContextMenuPolicy(Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(lambda pos, v=view: self.context_menu(v, pos))
        return view

    # ---------- data ----------
    def refresh(self):
        self.last_refresh = time.monotonic()
        self.tasks.submit(repo.list_changes, self._on_list, self.path)
        branch = self.p.status.branch if self.p.status else None
        self.branch.setText(f"⎇  {branch}" if branch else "detached HEAD")

    def _on_list(self, result, error):
        if error:
            self.status.setText("Could not read the repository status")
            self.diff.set_message(str(error))
            return
        if result == (self.staged, self.unstaged):
            return  # Nothing changed: leave lists, selection and diff alone.
        self.staged, self.unstaged = result
        # Files committed, discarded or stashed elsewhere drop out of the ticked set.
        self.checked.intersection_update({f.path for f in self.staged + self.unstaged})
        self.diff_cache.clear()
        self.content_hits = None
        if self.in_content.isChecked() and self.search.text().strip():
            self.start_content_search()
        self.populate()

    def visible(self, fc: FileChange) -> bool:
        if self.ext and extension(fc.path) != self.ext:
            return False
        term = self.search.text().strip().lower()
        if not term:
            return True
        if term in fc.path.lower() or (fc.orig and term in fc.orig.lower()):
            return True
        return self.in_content.isChecked() and self.content_hits is not None and fc.key in self.content_hits

    def set_ext(self, ext: str):
        self.ext = ext
        self.populate()

    def populate(self):
        keep = self.current.key if self.current else None
        for view, files in ((self.staged_view, self.staged), (self.unstaged_view, self.unstaged)):
            # Only the view holding the current file keeps a selection.
            owns = keep is not None and keep[0] == (view is self.staged_view)
            view.set_files([f for f in files if self.visible(f)], keep if owns else ("", ""))
        self.staged_ext.set_files(self.staged, self.ext)
        self.unstaged_ext.set_files(self.unstaged, self.ext)
        shown_s, shown_u = self.staged_view.file_count(), self.unstaged_view.file_count()
        self.staged_title.setText(self._title("Staged", shown_s, len(self.staged)))
        self.unstaged_title.setText(self._title("Changes", shown_u, len(self.unstaged)))
        all_files = {f.key: f for f in self.staged + self.unstaged}
        if keep in all_files and self.visible(all_files[keep]):
            self.current = all_files[keep]
            self.show_diff(self.current)
        else:
            self.current = None
            if not (self.unstaged_view.select_first() or self.staged_view.select_first()):
                self.diff.set_message("No changes: working tree clean" if not all_files
                                      else "No file matches the filters")
        self.update_buttons()
        self.update_checks()

    @staticmethod
    def _title(name: str, shown: int, total: int) -> str:
        count = f"{shown} / {total}" if shown != total else str(total)
        return f"{name} <span style='color:{C['faint']}'>&nbsp;{count}</span>"

    def on_search(self):
        if self.in_content.isChecked() and self.search.text().strip():
            self.status.setText("Searching in content…")
            self.search_timer.start()
        else:
            self.status.setText("")
        self.populate()

    def start_content_search(self):
        term = self.search.text().strip()
        if not term or not self.in_content.isChecked():
            return
        self.search_gen += 1
        gen = self.search_gen
        self.tasks.submit(_content_search, lambda hits, err: self._on_content(gen, hits),
                          self.path, self.staged + self.unstaged, term)

    def _on_content(self, gen: int, hits):
        if gen != self.search_gen:
            return  # A newer search superseded this one.
        self.content_hits = hits or set()
        self.status.setText(f"{len(self.content_hits)} file(s) contain the text")
        self.populate()

    # ---------- selection & diff ----------
    def on_selection(self, view: FileView):
        if not view.selectedItems():
            return
        other = self.unstaged_view if view is self.staged_view else self.staged_view
        other.blockSignals(True)
        other.clearSelection()
        other.blockSignals(False)
        fc = view.current_file() or next(iter(view.selected_files()), None)
        if fc and (self.current is None or fc.key != self.current.key):
            self.current = fc
            self.show_diff(fc)

    def show_diff(self, fc: FileChange):
        ws, full = self.diff.ignore_ws, self.diff.full_file
        key = (*fc.key, ws, full)
        if key in self.diff_cache:
            self._display(fc, self.diff_cache[key])
            return
        # Refreshing the file already on screen: keep it visible (and scrolled) until the new diff arrives.
        if not (self.diff.data is not None and self.diff.filename == fc.path):
            self.diff.set_message("Loading…", fc.path)
        self.tasks.submit(_load_diff, lambda data, err: self._on_diff(fc, key, data, err), self.path, fc, ws, full)

    def _on_diff(self, fc: FileChange, key, data, error):
        if error:
            if self.current and self.current.key == fc.key:
                self.diff.set_message(str(error), fc.path)
            return
        self.diff_cache[key] = data
        if self.current and self.current.key == fc.key:
            self._display(fc, data)

    def _display(self, fc: FileChange, data):
        shown = self.diff.data
        same = (shown is not None and self.diff.filename == fc.path and self._shown_key == fc.key
                and getattr(shown, "signature", None) == getattr(data, "signature", object()))
        if not same:
            self.diff.set_diff(fc.path, data, keep_scroll=True)
        self._shown_key = fc.key
        # Partial patches only for plain modifications; new, deleted, renamed or untracked files go whole.
        if fc.code != "M" or fc.orig:
            self.diff.set_actions([])
        else:
            self.diff.set_actions(["unstage"] if fc.staged else ["discard", "stage"])
        term = self.search.text().strip()
        if term and self.in_content.isChecked():
            self.diff.set_find(term)

    # ---------- stash of ticked files ----------
    def set_checked(self, paths: list[str], on: bool):
        if on:
            self.checked.update(paths)
        else:
            self.checked.difference_update(paths)
        self.update_checks()

    def update_checks(self):
        for view in (self.staged_view, self.unstaged_view):
            view.viewport().update()
        for view, box in ((self.staged_view, self.staged_all), (self.unstaged_view, self.unstaged_all)):
            hits = sum(1 for f in view.files if f.path in self.checked)
            box.setEnabled(bool(view.files))
            box.setCheckState(Qt.Unchecked if not hits else Qt.Checked if hits == len(view.files)
                              else Qt.PartiallyChecked)
        n = len(self.checked)
        self.stash_bar.setVisible(bool(n))
        self.stash_count.setText(f"{n} file{'s' if n != 1 else ''} ticked")
        self.stash_btn.setText(f"Stash {n} file{'s' if n != 1 else ''}…")
        self.stash_btn.setEnabled(bool(n) and not self.busy)

    def stash_checked(self):
        files = [f for f in self.staged + self.unstaged if f.path in self.checked]
        if self.busy or not files:
            return
        conflicts = sorted({f.path for f in files if f.code == "U"})
        if conflicts:
            QMessageBox.warning(self, "Stash files", "Files with merge conflicts cannot be stashed:\n\n"
                                + "\n".join(conflicts[:12]) + "\n\nResolve them first, or untick them.")
            return
        dialog = StashFilesDialog(self, files)
        if not dialog.exec():
            return
        n, keep = len({f.path for f in files}), dialog.keep
        plural = "s" if n != 1 else ""
        self.set_busy(True, f"Stashing {n} file{plural}…")

        def done(_sha, err):
            if not err:
                self.checked.clear()
            self._after(err, "Stash files", "" if err else
                        f"Stashed {n} file{plural}, " + ("changes kept" if keep else "files reverted"))

        self.tasks.submit(repo.stash_selected, done, self.path, files, dialog.message.text().strip(), keep)

    # ---------- actions ----------
    def toggle(self, view: FileView, files: list[FileChange]):
        if len(files) == 1 and files[0].code == "U":
            self.main.open_merge_tool(self.p, files[0].path)  # A conflict is resolved, not staged.
            return
        if view is self.staged_view:
            self.unstage(files)
        else:
            self.stage(files)

    def stage(self, files):
        if files:
            self.run(repo.stage, files, "Staging")

    def unstage(self, files):
        if files:
            self.run(repo.unstage, files, "Unstaging")

    def discard(self, files):
        if not files:
            return
        untracked = sum(1 for f in files if f.code == "?")
        names = "\n".join(f.path for f in files[:12]) + ("\n…" if len(files) > 12 else "")
        extra = f"\n\n{untracked} untracked file(s) will be deleted." if untracked else ""
        box = QMessageBox(QMessageBox.Warning, "Discard changes",
                          f"Discard local changes in {len(files)} file(s)?\n\n{names}{extra}\n\n"
                          "This cannot be undone.", QMessageBox.Cancel, self)
        confirm = box.addButton("Discard", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is confirm:
            self.run(repo.discard, files, "Discarding changes")

    def keep_side(self, fc: FileChange, side: str, label: str):
        self.set_busy(True, f"Keeping the {label} version…")
        self.tasks.submit(rebase.take_side, lambda _r, err: self._after(err, "Resolve conflict"), self.path,
                          fc.path, side)

    def ignore(self, files: list[FileChange]):
        if not files or self.busy:
            return
        dialog = IgnoreDialog(self, files)
        if not dialog.exec():
            return
        patterns, local, untrack = dialog.patterns(), dialog.local_only(), dialog.untracked_paths()

        def work():
            target = ""
            for i, pattern in enumerate(patterns):
                target = repo.add_ignore(self.path, pattern, local, untrack if i == 0 else [])
            return target

        self.set_busy(True, "Updating ignore rules…")
        self.tasks.submit(work, lambda target, err: self._after(
            err, "Ignore", f"Added to {os.path.relpath(target, self.path)}" if target else ""))

    def apply_patch(self, selection: dict, action: str):
        fc, data = self.current, self.diff.data
        if self.busy or fc is None or data is None:
            return
        whole = all(v is None for v in selection.values())
        what = "hunk" if whole and len(selection) == 1 else "selected lines"
        if action == "discard":
            box = QMessageBox(QMessageBox.Warning, "Discard changes",
                              f"Discard the {what} in {fc.path}?\n\nThis cannot be undone.",
                              QMessageBox.Cancel, self)
            confirm = box.addButton("Discard", QMessageBox.DestructiveRole)
            box.setDefaultButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is not confirm:
                return
        label = {"stage": "Staging", "unstage": "Unstaging", "discard": "Discarding"}[action] + f" {what}"
        self.set_busy(True, f"{label}…")
        self.tasks.submit(repo.apply_selection, lambda _r, err: self._after(err, label), self.path, fc,
                          self.diff.full_file, selection, action, list(data.hunk_sizes))

    def run(self, fn, files, label: str):
        if self.busy:
            return
        self.set_busy(True, f"{label}…")
        self.tasks.submit(fn, lambda _r, err: self._after(err, label), self.path, files)

    def _after(self, error, label: str, message: str = ""):
        self.set_busy(False, message)
        if error:
            ErrorDialog(explain(str(error), label), self.p.name, self).exec()
        self.refresh()
        self.changed.emit()

    def do_commit(self, then_push: bool):
        msg = self.message.toPlainText().strip()
        if self.busy or not msg or not self.staged:
            return
        self.set_busy(True, "Committing…")
        self.tasks.submit(repo.commit, lambda sha, err: self._on_commit(sha, err, then_push), self.path, msg)

    def _on_commit(self, sha, error, then_push: bool):
        if not error:
            self.message.clear()
        self._after(error, "Commit", f"Committed {sha}" if not error else "")
        if not error and then_push:
            self.main.push_project(self.p)

    def set_busy(self, busy: bool, text: str = ""):
        self.busy = busy
        self.status.setText(text)
        self.update_buttons()

    def update_buttons(self):
        ok = bool(self.staged) and bool(self.message.toPlainText().strip()) and not self.busy
        self.commit_btn.setEnabled(ok)
        self.commit_push_btn.setEnabled(ok)
        n = len(self.staged)
        self.commit_btn.setText(f"Commit {n} file{'s' if n != 1 else ''}" if n else "Commit")
        if hasattr(self, "stash_btn"):
            self.stash_btn.setEnabled(bool(self.checked) and not self.busy)

    def context_menu(self, view: FileView, pos):
        files = view.item_files_at(pos)
        if not files:
            return
        fc = files[0]
        full = os.path.join(self.path, fc.path)
        single = len(files) == 1
        tracked = fc.code not in ("?", "A")
        menu = QMenu(self)
        entries = [(("Unstage" if view is self.staged_view else "Stage") + ("" if single else f" {len(files)} files"),
                    lambda: self.toggle(view, files), True)]
        if single and fc.code == "U":
            left, right = rebase.conflict_sides(self.path)
            entries = [("Resolve conflict…", lambda: self.main.open_merge_tool(self.p, fc.path), True),
                       (f"Keep {left} version", lambda: self.keep_side(fc, "ours", left), True),
                       (f"Keep {right} version", lambda: self.keep_side(fc, "theirs", right), True),
                       (None, None, True)] + entries[1:]
        if view is self.unstaged_view:
            entries.append(("Discard changes…", lambda: self.discard(files), True))
            entries.append(("Ignore…", lambda: self.ignore(files), True))
        entries += [
            (None, None, True),
            ("File history && blame", lambda: self.main.open_file_history(self.p, fc.path), single and tracked),
            ("Open file", lambda: os.path.exists(full) and os.startfile(full), single),
            ("Show in Explorer", lambda: subprocess.Popen(["explorer", "/select,", os.path.normpath(full)]), single),
            ("Copy path", lambda: QGuiApplication.clipboard().setText("\n".join(f.path for f in files)), True),
        ]
        for text, fn, enabled in entries:
            if text is None:
                menu.addSeparator()
                continue
            act = QAction(text, menu)
            act.setEnabled(enabled)
            act.triggered.connect(fn)
            menu.addAction(act)
        menu.exec(view.viewport().mapToGlobal(pos))

    def changeEvent(self, event):
        # Files edited in another app while this window was in the background.
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow() and not self.busy
                and time.monotonic() - self.last_refresh > 2):
            self.refresh()
        super().changeEvent(event)
