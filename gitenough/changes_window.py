import os
import subprocess
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from . import ai, never_commit, rebase, repo
from .config import Config
from .diff_view import DiffView, parse_diff
from .errors import explain
from .file_editor import FileEditor
from .file_view import ExtensionBar, FileView, extension
from .ignore_dialog import IgnoreDialog
from .image_view import ImageDiffView, is_image, load_pair
from .never_commit_dialog import NeverCommitDialog
from .repo import FileChange
from .style import C
from .widgets import ElidedLabel, ErrorDialog, Spinner, ai_error_dialog, icon_button, keep_size
from .open_menu import OpenButton


def _list(path: str, hide: bool):
    staged, unstaged = repo.list_changes(path, hide)
    return staged, unstaged, never_commit.summary(path) if never_commit.has_entries(path) else (0, 0, 0, 0)


def _load_diff(path: str, fc: FileChange, ws: bool, full: bool, hide: bool):
    text = repo.change_diff(path, fc, ws, full, hide)
    data = parse_diff(text)
    data.signature = hash(text)  # lets a refresh skip re-rendering an unchanged diff
    return data


def _content_search(path: str, files: list[FileChange], term: str, hide: bool) -> set:
    term = term.lower()
    hits = set()
    for key, text in repo.all_diffs(path, files, hide).items():
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


POLL_MS = 5000  # periodic refresh of an open Changes window


class _AiBridge(QObject):
    """Carries the commit message, piece by piece, from the Claude worker thread to the window."""

    text = Signal(str)


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
        self.ai_before = None  # message typed before Claude started writing; None when Claude is not writing
        self.show_hidden = False  # never-commit lines shown in the list and the diffs
        self.kept = (0, 0, 0, 0)  # never_commit.summary
        self._rebuild = False

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
        refresh = QPushButton("⟳ Refresh")
        refresh.setToolTip("Read the files and their diffs again (F5); also done every few seconds")
        refresh.clicked.connect(self.refresh)
        hl.addWidget(refresh)
        hl.addWidget(OpenButton(self.main, project))
        root.addWidget(header)
        root.addWidget(self._kept_bar())

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(1)
        split.addWidget(self._left_panel())
        self.diff = DiffView()
        self.diff.options_changed.connect(lambda: self.current and self.show_diff(self.current))
        self.diff.patch_requested.connect(self.apply_patch)
        self.edit_btn = QPushButton("✎ Edit")
        self.edit_btn.setObjectName("rowAction")
        self.edit_btn.setToolTip("Edit this file here (Ctrl+E); saving evaluates the changes again")
        self.edit_btn.clicked.connect(self.edit_current)
        self.diff.add_title_widget(self.edit_btn)
        self.editor = FileEditor()
        self.editor.saved.connect(self._on_saved)
        self.editor.closed.connect(self._close_editor)
        self.center = QStackedWidget()
        self.center.addWidget(self.diff)
        self.center.addWidget(self.editor)
        self.images = ImageDiffView()  # page 2: images, before and after
        self.center.addWidget(self.images)
        self._image_shown = None  # (file key, before, after) on the image page
        split.addWidget(self.center)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 1020])
        root.addWidget(split, 1)

        QShortcut(QKeySequence("F5"), self, self.refresh)
        QShortcut(QKeySequence("Ctrl+E"), self, self.edit_current)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.search.setFocus())
        QShortcut(QKeySequence("Ctrl+Return"), self, lambda: self.do_commit(False))
        self.search_timer = QTimer(self, singleShot=True, interval=300)
        self.search_timer.timeout.connect(self.start_content_search)
        # Besides the file watcher (off in Settings, or blind to a network drive): a cheap git status.
        self.poll_timer = QTimer(self, interval=POLL_MS)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start()
        self.refresh()

    # ---------- layout ----------
    def _kept_bar(self) -> QWidget:
        """Shown while the repository has never-commit lines."""
        self.kept_widget = QWidget()
        self.kept_widget.setObjectName("diffNotes")
        bar = QHBoxLayout(self.kept_widget)
        bar.setContentsMargins(20, 6, 16, 6)
        self.kept_label = QLabel("")
        self.kept_label.setTextFormat(Qt.RichText)
        self.kept_show = QCheckBox("Show them")
        self.kept_show.setToolTip("List and diff the files with their never-commit lines (read only for those lines)")
        self.kept_show.toggled.connect(self._toggle_hidden)
        manage = QPushButton("Manage…")
        manage.setObjectName("rowAction")
        manage.clicked.connect(self.manage_kept)
        bar.addWidget(self.kept_label, 1)
        bar.addWidget(self.kept_show)
        bar.addWidget(manage)
        self.kept_widget.hide()
        return self.kept_widget

    def _update_kept(self):
        found, files, lost, whole = self.kept
        self.kept_widget.setVisible(bool(found or lost or whole))
        parts = []
        if found:
            parts.append(f"{found} never-commit change{'s' if found != 1 else ''} hidden in {files} "
                         f"file{'s' if files != 1 else ''}")
        if whole:
            parts.append(f"{whole} new file{'s' if whole != 1 else ''} never committed")
        text = "🔒 " + (" · ".join(parts) or "No never-commit change in the files")
        if lost:
            text += (f" · <span style='color:{C['orange']}'>{lost} no longer match{'es' if lost == 1 else ''} "
                     "the file</span>")
        self.kept_label.setText(text)

    def _toggle_hidden(self, on: bool):
        self.show_hidden = on
        self.diff_cache.clear()
        self._rebuild = True  # same files maybe, other diffs
        self.refresh()

    def manage_kept(self):
        NeverCommitDialog(self, self.path, self.p.name).exec()
        self.diff_cache.clear()
        self.refresh()
        self.changed.emit()

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
        # The draft (typed or suggested by Claude) outlives the window: it comes back at the next opening.
        self.message.setPlainText(Config.load_draft(self.path))
        self.draft_timer = QTimer(self, singleShot=True, interval=600)
        self.draft_timer.timeout.connect(
            lambda: self.ai_before is None and Config.save_draft(self.path, self.message.toPlainText()))
        self.message.textChanged.connect(self.draft_timer.start)
        lay.addWidget(self.message)
        buttons = QHBoxLayout()
        self.suggest_btn = QPushButton("✨ Suggest")
        self.suggest_btn.setToolTip("Write a commit message from the staged changes with Claude "
                                    f"({self.main.config.ai_commit_model or 'default model'}, "
                                    "through your Claude Code sign-in)")
        self.suggest_btn.clicked.connect(self.suggest_message)
        self.suggest_btn.setVisible(ai.find_cli() is not None)
        self.ai_spinner = Spinner(16)
        self.ai_spinner.hide()
        self.ai_bridge = _AiBridge(self)
        self.ai_bridge.text.connect(self._on_ai_text)
        buttons.addWidget(self.ai_spinner)
        buttons.addWidget(self.suggest_btn)
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
    def _poll(self):
        if self.isVisible() and not self.isMinimized() and not self.busy and time.monotonic() - self.last_refresh > 2:
            self.refresh()

    def refresh(self):
        self.last_refresh = time.monotonic()
        self.tasks.submit(_list, self._on_list, self.path, not self.show_hidden)
        branch = self.p.status.branch if self.p.status else None
        self.branch.setText(f"⎇  {branch}" if branch else "detached HEAD")

    def _on_list(self, result, error):
        if error:
            self.status.setText("Could not read the repository status")
            self.diff.set_message(str(error))
            return
        *lists, kept = result
        if kept != self.kept:
            self.kept = kept
            self.diff_cache.clear()
            self._update_kept()
        elif tuple(lists) == (self.staged, self.unstaged) and not self._rebuild:
            # Same files, but the one on screen may have changed again: its diff is read anew, and only
            # re-rendered when it differs (lists, selection and scroll stay).
            self.diff_cache.clear()
            if self.current is not None and not self.editing:
                self.show_diff(self.current)
            return
        self._rebuild = False
        self.staged, self.unstaged = lists
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
            if self.center.currentIndex() == 2:
                self.center.setCurrentIndex(0)  # no image selected any more
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
                          self.path, self.staged + self.unstaged, term, not self.show_hidden)

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
        if self.editing:
            if not self.editor.confirm_leave():
                self._select(self.current)  # stay on the file with unsaved edits
                return
            self.center.setCurrentIndex(0)
        other = self.unstaged_view if view is self.staged_view else self.staged_view
        other.blockSignals(True)
        other.clearSelection()
        other.blockSignals(False)
        fc = view.current_file() or next(iter(view.selected_files()), None)
        if fc and (self.current is None or fc.key != self.current.key):
            self.current = fc
            self.show_diff(fc)

    # ---------- edit a file in place ----------
    @property
    def editing(self) -> bool:
        return self.center.currentIndex() == 1

    def _editable(self, fc: FileChange | None) -> bool:
        return (fc is not None and fc.code != "U" and not is_image(fc.path)
                and os.path.isfile(os.path.join(self.path, fc.path)))

    def edit_current(self):
        fc = self.current
        if self.editing or fc is None:
            return
        if fc.code == "U":
            self.main.open_merge_tool(self.p, fc.path)  # conflicts have their own editor
            return
        error = self.editor.open_file(self.path, fc.path)
        if error:
            QMessageBox.information(self, "Edit", f"{fc.path}\n\n{error}")
            return
        self.center.setCurrentIndex(1)

    def _on_saved(self, rel: str):
        # Diffs are cached per file: the saved one must be computed again, even if the list looks the same.
        self.diff_cache.clear()
        self.status.setText(f"Saved {rel}")
        self.refresh()
        if self.current is not None:
            self.show_diff(self.current)

    def _close_editor(self):
        self.center.setCurrentIndex(0)
        if self.current is not None:
            self.show_diff(self.current)

    def _select(self, fc: FileChange | None):
        view = self.staged_view if fc is not None and fc.staged else self.unstaged_view
        for v in (self.staged_view, self.unstaged_view):
            v.blockSignals(True)
            v.clearSelection()
        if fc is not None:
            view.select_path(fc.path)
        for v in (self.staged_view, self.unstaged_view):
            v.blockSignals(False)

    def show_diff(self, fc: FileChange):
        if is_image(fc.path):
            self.tasks.submit(load_pair, lambda pair, err: self._on_images(fc, pair, err), self.path, fc)
            return
        if self.center.currentIndex() == 2:
            self.center.setCurrentIndex(0)
        ws, full = self.diff.ignore_ws, self.diff.full_file
        key = (*fc.key, ws, full)
        if key in self.diff_cache:
            self._display(fc, self.diff_cache[key])
            return
        # Refreshing the file already on screen: keep it visible (and scrolled) until the new diff arrives.
        if not (self.diff.data is not None and self.diff.filename == fc.path):
            self.diff.set_message("Loading…", fc.path)
        self.tasks.submit(_load_diff, lambda data, err: self._on_diff(fc, key, data, err), self.path, fc, ws, full,
                          not self.show_hidden)

    def _on_images(self, fc: FileChange, pair, error):
        if not (self.current and self.current.key == fc.key) or self.editing:
            return
        if error:
            self.center.setCurrentIndex(0)
            self.diff.set_message(str(error), fc.path)
            return
        self.edit_btn.setEnabled(False)
        shown = (fc.key, *pair)
        if shown != self._image_shown:  # the periodic refresh repaints only a changed image
            self._image_shown = shown
            self.images.show_pair(fc.path, pair[0], pair[1], fc.staged)
        self.center.setCurrentIndex(2)

    def _on_diff(self, fc: FileChange, key, data, error):
        if error:
            if self.current and self.current.key == fc.key:
                self.diff.set_message(str(error), fc.path)
            return
        self.diff_cache[key] = data
        if self.current and self.current.key == fc.key:
            self._display(fc, data)

    def _display(self, fc: FileChange, data):
        self.edit_btn.setEnabled(self._editable(fc))
        shown = self.diff.data
        same = (shown is not None and self.diff.filename == fc.path and self._shown_key == fc.key
                and getattr(shown, "signature", None) == getattr(data, "signature", object()))
        if not same:
            self.diff.set_diff(fc.path, data, keep_scroll=True)
        self._shown_key = fc.key
        # Partial patches only for plain modifications; new, deleted, renamed or untracked files go whole.
        if fc.code != "M" or fc.orig:
            self.diff.set_actions([])
        elif fc.staged:
            self.diff.set_actions(["unstage"])
        elif self.show_hidden:
            # The diff shows the never-commit lines, but patches apply to the file without them.
            kept = any(e["file"] == fc.path for e in never_commit.entries(self.path))
            self.diff.set_actions([] if kept else ["discard", "stage"])
        else:
            self.diff.set_actions(["never", "discard", "stage"])
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

    def never_files(self, files: list[FileChange]):
        """Whole files kept out of commits: new files through info/exclude, modified ones as all their lines."""
        if self.busy or not files:
            return
        n = len(files)

        def work():
            new = [f.path for f in files if f.code == "?"]
            if new:
                never_commit.add_files(self.path, new)
            for f in files:
                if f.code == "M":
                    never_commit.add(self.path, f.path, False, None, None)

        self.set_busy(True, f"Hiding {n} file{'s' if n != 1 else ''}…")
        self.tasks.submit(work, lambda _r, err: self._after(
            err, "Never commit", f"{n} file{'s' if n != 1 else ''} will never be committed"))

    def allow_files(self, files: list[FileChange]):
        never_commit.forget_files(self.path, [f.path for f in files])
        self.diff_cache.clear()
        self.refresh()
        self.changed.emit()

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
        if action == "never":
            self.set_busy(True, f"Hiding the {what}…")
            self.tasks.submit(never_commit.add, lambda n, err: self._after(
                err, "Never commit", f"{n} change{'s' if n != 1 else ''} will never be committed" if n else ""),
                self.path, fc.path, self.diff.full_file, selection, list(data.hunk_sizes))
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

    def suggest_message(self):
        if self.ai_before is not None:
            self.ai_handle.cancel()  # the button reads "Stop" while Claude writes
            return
        if self.busy or not self.staged:
            return
        model = self.main.config.ai_commit_model
        self.status.setText(f"Claude ({model or 'default model'}) is reading the staged changes…")
        self.ai_handle = ai.Handle()
        path, bridge = self.path, self.ai_bridge
        # Claude's answer appears in the box as it is written; Ctrl+Z afterwards brings this text back.
        self.ai_before = self.message.toPlainText()
        self.message.setUndoRedoEnabled(False)
        self.message.setReadOnly(True)
        self.message.clear()
        self.message.setPlaceholderText("Claude is reading the staged changes…")
        self.ai_spinner.show()
        self.suggest_btn.setText("■ Stop")
        self.update_buttons()

        def piece(text):
            try:
                bridge.text.emit(text)
            except RuntimeError:
                pass  # window closed: the final answer still goes to the draft

        def done(text, error):
            if text and not error:
                Config.save_draft(path, text)  # kept even when this window is closed meanwhile
            try:
                self._on_suggestion(text, error)
            except RuntimeError:
                pass  # window closed before Claude answered: the draft waits for the next opening

        self.tasks.submit_ai(ai.commit_message, done, path, model, self.ai_handle, piece)

    def _on_ai_text(self, text: str):
        if self.ai_before is None:
            return
        if not self.message.toPlainText():
            self.status.setText("Claude is writing the commit message…")
        cursor = self.message.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.message.setTextCursor(cursor)

    def _on_suggestion(self, text, error):
        before, self.ai_before = self.ai_before or "", None
        self.ai_spinner.hide()
        self.suggest_btn.setText("✨ Suggest")
        self.message.setReadOnly(False)
        self.message.setPlaceholderText("Commit message  (Ctrl+Enter to commit)")
        self.message.setPlainText(before)  # undo is still off: this step is not recorded
        self.message.setUndoRedoEnabled(True)
        self.update_buttons()
        if error:
            self.status.setText("")
            if str(error) != "Cancelled":
                ai_error_dialog(self, str(error))
            return
        used = ", ".join(getattr(self, "ai_handle", None) and self.ai_handle.models or []) or "Claude"
        self.status.setText(f"Suggested by {used}: edit it before committing if needed")
        # Through the cursor, so Ctrl+Z brings back what was typed before.
        cursor = self.message.textCursor()
        cursor.select(cursor.SelectionType.Document)
        cursor.insertText(text)
        self.message.setFocus()

    def do_commit(self, then_push: bool):
        msg = self.message.toPlainText().strip()
        if self.busy or not msg or not self.staged:
            return
        self.set_busy(True, "Committing…")
        self.tasks.submit(repo.commit, lambda sha, err: self._on_commit(sha, err, then_push), self.path, msg)

    def _on_commit(self, sha, error, then_push: bool):
        if not error:
            self.message.clear()
            self.draft_timer.stop()
            Config.save_draft(self.path, "")
        self._after(error, "Commit", f"Committed {sha}" if not error else "")
        if not error and then_push:
            self.main.push_project(self.p)

    def set_busy(self, busy: bool, text: str = ""):
        self.busy = busy
        self.status.setText(text)
        self.update_buttons()

    def update_buttons(self):
        if not hasattr(self, "commit_btn"):
            return  # the restored draft fires textChanged while the panel is still being built
        ok = (bool(self.staged) and bool(self.message.toPlainText().strip()) and not self.busy
              and self.ai_before is None)  # never commit a message Claude is still writing
        self.commit_btn.setEnabled(ok)
        if hasattr(self, "suggest_btn"):
            self.suggest_btn.setEnabled(self.ai_before is not None or (bool(self.staged) and not self.busy))
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
            kept_new = set(never_commit.kept_files(self.path))
            if all(f.path in kept_new for f in files):
                entries.append(("Allow committing", lambda: self.allow_files(files), True))
            else:
                entries.append(("Never commit" + ("" if single else f" {len(files)} files"),
                                lambda: self.never_files(files), all(f.code in "?M" and not f.orig for f in files)))
        entries += [
            (None, None, True),
            ("File history && blame", lambda: self.main.open_file_history(self.p, fc.path), single and tracked),
            ("Edit here", lambda: self.edit_current(), single and self._editable(fc)),
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

    def closeEvent(self, event):
        if self.editing and not self.editor.confirm_leave():
            event.ignore()
            return
        if self.draft_timer.isActive():
            self.draft_timer.stop()
            Config.save_draft(self.path, self.message.toPlainText())
        if self.ai_before is not None:
            Config.save_draft(self.path, self.ai_before)  # Claude's answer replaces it when it arrives
        super().closeEvent(event)

    def changeEvent(self, event):
        # Files edited in another app while this window was in the background.
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow() and not self.busy
                and time.monotonic() - self.last_refresh > 2):
            self.refresh()
            if self.editing:
                self.editor.reload_if_clean()
        super().changeEvent(event)
