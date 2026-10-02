import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from . import ai, ai_review, ides, review_skill, vault
from .about import AboutPage
from .config import Config
from .git_ops import Credential, GitError, is_http, parse_url, same_repo, test_access
from .gitlab import gitlab_hosts, parse_mr_url
from .style import C
from .tasks import Tasks
from .widgets import keep_size


def parse_repo_lines(text: str) -> list[str]:
    seen, urls = set(), []
    for line in text.splitlines():
        url = line.strip()
        if url and not url.startswith("#") and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _host_path(url: str) -> tuple[str, str]:
    host, parts = parse_url(url)
    return host, "/".join(parts)


class HostRow(QFrame):
    def __init__(self, host: str, username: str, saved: bool, dialog: "SettingsDialog"):
        super().__init__()
        self.host, self.saved, self.removed, self.dialog = host, saved, False, dialog
        self.setStyleSheet(f"HostRow {{ background: {C['surface2']}; border: 1px solid {C['border']};"
                           f" border-radius: 10px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        name = QLabel(host)
        name.setStyleSheet("font-weight: 700; font-size: 10.5pt;")
        self.badge = QLabel()
        top.addWidget(name)
        top.addWidget(self.badge)
        top.addStretch()
        self.test_btn = QPushButton("Test")
        self.test_btn.setObjectName("rowAction")
        self.test_btn.clicked.connect(self.run_test)
        self.remove_btn = QPushButton("Remove token")
        self.remove_btn.setObjectName("rowAction")
        self.remove_btn.clicked.connect(self.remove)
        top.addWidget(self.test_btn)
        top.addWidget(self.remove_btn)
        lay.addLayout(top)

        fields = QHBoxLayout()
        self.user = QLineEdit(username)
        self.user.setPlaceholderText(vault.default_username(host))
        self.user.setToolTip("Username sent with the token (often ignored by the server)")
        self.user.setFixedWidth(180)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setClearButtonEnabled(True)
        fields.addWidget(self.user)
        fields.addWidget(self.token, 1)
        lay.addLayout(fields)

        self.result = QLabel()
        self.result.setObjectName("muted")
        self.result.setWordWrap(True)
        self.result.hide()
        lay.addWidget(self.result)
        self.refresh_badge()

    def refresh_badge(self):
        if self.saved and not self.removed:
            self.badge.setText("● token saved")
            self.badge.setStyleSheet(f"color: {C['green']}; font-size: 9pt;")
            self.token.setPlaceholderText("Leave empty to keep the current token")
        else:
            self.badge.setText("● no token")
            self.badge.setStyleSheet(f"color: {C['faint']}; font-size: 9pt;")
            self.token.setPlaceholderText("Personal Access Token (repository read access is enough)")
        self.remove_btn.setEnabled(self.saved and not self.removed)

    def remove(self):
        self.removed = True
        self.token.clear()
        self.refresh_badge()

    def credential(self) -> Credential | None:
        token = self.token.text().strip() or (None if self.removed else vault.get_token(self.host))
        if not token:
            return None
        return Credential(self.user.text().strip() or vault.default_username(self.host), token)

    def run_test(self):
        # A repository this token would serve: same host, and inside the group for a group token.
        url = next((u for u in self.dialog.current_urls() + self.dialog.repo_urls
                    if is_http(u) and vault.scope_for(*_host_path(u)) == self.host), None)
        if not url:
            self.show_result(False, "No HTTPS repository served by this token in the list.")
            return
        self.test_btn.setEnabled(False)
        self.show_result(None, f"Testing {url}…")
        self.dialog.tasks.submit_network(test_access, self._on_test, url, self.credential())

    def _on_test(self, result, error):
        try:
            self.test_btn.setEnabled(True)
            if isinstance(error, GitError):
                self.show_result(False, str(error))
            elif error:
                self.show_result(False, repr(error))
            else:
                self.show_result(True, result)
        except RuntimeError:
            pass  # Dialog closed before the test finished.

    def show_result(self, ok: bool | None, text: str):
        color = C["muted"] if ok is None else (C["green"] if ok else C["red"])
        self.result.setStyleSheet(f"color: {color}; font-size: 9pt;")
        self.result.setText(text)
        self.result.show()


class SettingsDialog(QDialog):
    def __init__(self, config: Config, tasks: Tasks, parent=None):
        super().__init__(parent)
        self.config, self.tasks = config, tasks
        # Repositories found on disk are not in config.repos: their hosts count for the access checks.
        self.repo_urls = [p.url for p in getattr(parent, "projects", [])]
        self.host_rows: dict[str, HostRow] = {}
        self.setWindowTitle("Settings")
        self.resize(760, 560)
        keep_size(self, self.config, "settings", persist=False)  # Saved with the form, or at exit.

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(14)
        title = QLabel("Settings")
        title.setObjectName("title")
        root.addWidget(title)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._general_tab(), "General")
        self.tabs.addTab(self._repos_tab(), "Repositories")
        self.tabs.addTab(self._access_tab(), "Access (PAT)")
        self.tabs.addTab(self._claude_tab(), "Claude")
        self.tabs.addTab(AboutPage(), "About")
        self.tabs.currentChanged.connect(lambda i: i == 2 and self.sync_hosts())
        root.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        root.addLayout(buttons)
        self.sync_hosts()

    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)
        return page, lay

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        return label

    def _general_tab(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(QLabel("Root folder"))
        row = QHBoxLayout()
        self.root_edit = QLineEdit(self.config.root)
        self.root_edit.setPlaceholderText(r"e.g. C:\Dev")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.root_edit, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        lay.addWidget(self._hint("Each repository is cloned into <root>\\<repository name>. "
                                 "An existing folder is detected and reused."))
        lay.addSpacing(12)
        lay.addWidget(QLabel("Base branches (priority order)"))
        self.base_edit = QLineEdit(", ".join(self.config.base_branches))
        self.base_edit.setPlaceholderText("develop, main, master")
        lay.addWidget(self.base_edit)
        lay.addWidget(self._hint("The first branch that exists in a repository is its base branch: it is flagged "
                                 "when you are elsewhere, and History shows it next to the current branch. "
                                 "If none exists, the remote default branch (origin/HEAD) is used."))
        lay.addSpacing(12)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Auto-fetch every"))
        self.fetch_spin = QSpinBox()
        self.fetch_spin.setRange(0, 240)
        self.fetch_spin.setSuffix(" min")
        self.fetch_spin.setSpecialValueText("Off")
        self.fetch_spin.setValue(self.config.auto_fetch_minutes)
        row2.addWidget(self.fetch_spin)
        row2.addStretch()
        lay.addLayout(row2)
        lay.addWidget(self._hint("Fetches the checked projects in the background so Behind states show up "
                                 "without clicking Refresh. Nothing is pulled automatically."))
        self.watch_cb = QCheckBox("Refresh a repository as soon as its files change")
        self.watch_cb.setChecked(self.config.watch_files)
        lay.addWidget(self.watch_cb)
        lay.addSpacing(12)
        lay.addWidget(QLabel("Visual Studio for solutions"))
        self.vs_combo = QComboBox()
        self.vs_combo.addItem("Newest installed (stable channel)", ides.VS_LATEST)
        for label, exe in ides.visual_studios():
            self.vs_combo.addItem(label, exe)
        if os.path.isfile(ides.VS_LAUNCHER):
            self.vs_combo.addItem("Visual Studio Version Selector (the version the solution asks for)",
                                  ides.VS_SELECTOR)
        self.vs_combo.addItem("Windows default app for .sln", ides.VS_WINDOWS)
        index = self.vs_combo.findData(self.config.visual_studio)
        if index < 0:  # a version since uninstalled: shown, and the newest one is used meanwhile
            self.vs_combo.addItem(f"{self.config.visual_studio} (not found)", self.config.visual_studio)
            index = self.vs_combo.count() - 1
        self.vs_combo.setCurrentIndex(index)
        lay.addWidget(self.vs_combo)
        lay.addWidget(self._hint("Opens the .sln and .slnx files of a repository."))
        lay.addSpacing(12)
        share = QHBoxLayout()
        export_btn = QPushButton("Export configuration…")
        export_btn.clicked.connect(self.export_config)
        import_btn = QPushButton("Import configuration…")
        import_btn.clicked.connect(self.import_config)
        share.addWidget(export_btn)
        share.addWidget(import_btn)
        share.addStretch()
        lay.addLayout(share)
        lay.addWidget(self._hint("Share the repository list, base branches and host user names with your team. "
                                 "Tokens are never exported; each person adds their own."))
        lay.addStretch()
        return page

    def _model_combo(self, value: str) -> QComboBox:
        box = QComboBox()
        box.setEditable(True)  # a full model name works too
        for model, label in ai.MODEL_CHOICES:
            box.addItem(label, model)
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)
        else:
            box.setEditText(value)
        return box

    @staticmethod
    def _model_of(box: QComboBox) -> str:
        index = box.findText(box.currentText())
        return box.itemData(index) if index >= 0 else box.currentText().strip()

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight: 700; font-size: 10.5pt; margin-top: 6px;")
        return label

    def _claude_tab(self) -> QWidget:
        """Everything Claude in one page: sign-in, commit messages, merge request reviews."""
        page, lay = self._page()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # long text wraps instead
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        holder = QWidget()
        holder.setObjectName("claudePage")
        holder.setStyleSheet("QWidget#claudePage { background: transparent; }")  # not its buttons
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 6, 0)
        inner.setSpacing(10)
        scroll.setWidget(holder)
        lay.addWidget(scroll)

        inner.addWidget(self._section("Claude Code"))
        inner.addWidget(self._hint(
            "GitEnough uses Claude through Claude Code installed on this PC, signed in with your own Claude "
            "subscription: no API key. Usage counts against that subscription."))
        row = QHBoxLayout()
        self.ai_state = QLabel("Claude Code: checking…")
        self.ai_login = QPushButton("Sign in to Claude")
        self.ai_login.clicked.connect(ai.open_login)
        self.ai_login.hide()
        row.addWidget(self.ai_state, 1)
        row.addWidget(self.ai_login)
        inner.addLayout(row)
        self.tasks.submit(ai.status, self._on_ai_status)
        ver_row = QHBoxLayout()
        self.cli_version = QLabel("")
        self.cli_version.setObjectName("muted")
        update_cli = QPushButton("Update Claude Code")
        update_cli.setToolTip("Runs `claude update` in a console. Model aliases (haiku, sonnet, opus) map to the "
                              "newest models the installed version knows: update it to get the latest ones.")
        update_cli.clicked.connect(ai.open_update)
        ver_row.addWidget(self.cli_version, 1)
        ver_row.addWidget(update_cli)
        inner.addLayout(ver_row)
        self.tasks.submit(ai.cli_version, lambda v, _e: self.cli_version.setText(
            f"Installed version: {v}" if v else "Version unknown"))

        inner.addWidget(self._section("Models"))
        inner.addWidget(QLabel("Commit messages"))
        self.commit_model = self._model_combo(self.config.ai_commit_model)
        inner.addWidget(self.commit_model)
        inner.addWidget(QLabel("Merge request reviews"))
        self.review_model = self._model_combo(self.config.ai_review_model)
        inner.addWidget(self.review_model)
        inner.addWidget(self._hint("An alias always takes the newest model of that family. A full model name "
                                   "(e.g. claude-sonnet-5) works too."))

        inner.addWidget(self._section("Review skill (optional)"))
        inner.addWidget(self._hint(
            "A GitLab project whose CI builds .skill files as job artifacts. GitEnough lists the skills of its "
            "newest successful pipeline, installs the one you pick for the Claude Code CLI "
            "(~/.claude/skills; Claude Desktop is not touched), updates it at each Refresh, and warns "
            "you when it disappears. Empty: GitEnough's built-in review method."))
        src_row = QHBoxLayout()
        self.skill_source = QLineEdit(self.config.review_skill_source)
        self.skill_source.setPlaceholderText("https://gitlab.example.com/group/skills-project")
        look = QPushButton("Look for skills")
        look.clicked.connect(self._look_for_skills)
        src_row.addWidget(self.skill_source, 1)
        src_row.addWidget(look)
        inner.addLayout(src_row)
        pick_row = QHBoxLayout()
        self.skill_combo = QComboBox()
        self.skill_combo.addItem("None: built-in review method", None)
        current = self.config.review_skill
        if current:
            info = review_skill.installed_info(current)
            label = f"{current}  ·  installed" + (f" from pipeline #{info['pipeline']}" if info else " (missing)")
            self.skill_combo.addItem(label, current)
            self.skill_combo.setCurrentIndex(1)
        use = QPushButton("Use this skill")
        use.setObjectName("primary")
        use.clicked.connect(self._use_skill)
        pick_row.addWidget(self.skill_combo, 1)
        pick_row.addWidget(use)
        inner.addLayout(pick_row)
        self.skill_state = QLabel("")
        self.skill_state.setWordWrap(True)
        self.skill_state.setObjectName("muted")
        inner.addWidget(self.skill_state)
        self.found_skills: list = []

        access_head = QHBoxLayout()
        access_head.addWidget(self._section("Access"))
        access_head.addStretch()
        recheck = QPushButton("Check again")
        recheck.setObjectName("rowAction")
        recheck.clicked.connect(self._check_access)
        access_head.addWidget(recheck)
        inner.addLayout(access_head)
        self.access = QLabel("Checking…")
        self.access.setTextFormat(Qt.RichText)
        self.access.setWordWrap(True)
        inner.addWidget(self.access)

        inner.addWidget(self._section("Review clones"))
        cache_row = QHBoxLayout()
        self.cache_label = QLabel("Measuring…")
        self.cache_label.setObjectName("muted")
        self.cache_clean = QPushButton("Clean all")
        self.cache_clean.setObjectName("danger")
        self.cache_clean.clicked.connect(self._clean_cache)
        cache_row.addWidget(self.cache_label, 1)
        cache_row.addWidget(self.cache_clean)
        inner.addLayout(cache_row)
        inner.addWidget(self._hint(
            "AI reviews read the code from GitEnough's own copies of the projects, never from your working "
            "clones. They hold no file history and no Git LFS content, and are refreshed at each review. "
            r"Folder: %LOCALAPPDATA%\GitEnough\review-cache"))
        inner.addStretch()
        self._measure_cache()
        self._check_access()
        return page

    # ---------- review skill ----------
    def _look_for_skills(self):
        url = self.skill_source.text().strip()
        if not url:
            return
        self.skill_state.setText("Looking at the newest successful pipelines and their artifacts…")

        def done(skills, error):
            if error:
                self.skill_state.setText(f"<span style='color:{C['red']}'>●</span>  {error}")
                self.skill_state.setTextFormat(Qt.RichText)
                return
            self.found_skills = skills
            keep = self.skill_combo.currentData()
            self.skill_combo.clear()
            self.skill_combo.addItem("None: built-in review method", None)
            for sk in skills:
                self.skill_combo.addItem(sk.label, sk.name)
                self.skill_combo.setItemData(self.skill_combo.count() - 1, sk.description or sk.file, Qt.ToolTipRole)
            index = self.skill_combo.findData(keep)
            self.skill_combo.setCurrentIndex(index if index >= 0 else (1 if skills else 0))
            self.skill_state.setTextFormat(Qt.PlainText)
            self.skill_state.setText(f"{len(skills)} skill(s) found in pipeline #{skills[0].pipeline}" if skills else
                                     "No .skill file in the artifacts of the newest successful pipelines.")
            self._check_access()

        self.tasks.submit_api(lambda: review_skill.Source(url).latest_skills(), done)

    def _use_skill(self):
        name = self.skill_combo.currentData()
        url = self.skill_source.text().strip()
        old = self.config.review_skill
        if not name:
            if old:
                review_skill.uninstall(old)
            self.config.review_skill, self.config.review_skill_source = "", url
            self.config.save()
            self.skill_state.setTextFormat(Qt.PlainText)
            self.skill_state.setText("No review skill: the built-in review method is used.")
            return
        self.skill_state.setTextFormat(Qt.PlainText)
        self.skill_state.setText(f"Installing {name}…")

        def done(result, error):
            result = result or review_skill.CheckResult("error", str(error))
            if result.status in ("error", "missing"):
                self.skill_state.setTextFormat(Qt.RichText)
                self.skill_state.setText(f"<span style='color:{C['red']}'>●</span>  {result.message}")
                return
            if old and old != name:
                review_skill.uninstall(old)
            self.config.review_skill, self.config.review_skill_source = name, url
            self.config.save()  # installed already: the choice holds even if this dialog is cancelled
            sk = result.skill
            self.skill_state.setText(f"Using {name}" + (f", pipeline #{sk.pipeline}" if sk else "")
                                     + f": {review_skill.installed_dir(name)}")

        self.tasks.submit_api(review_skill.check, done, url, name)

    def _check_access(self):
        urls = [u for u in self.current_urls() + self.repo_urls if u.lower().startswith("http")]
        hosts = {parse_url(u)[0] for u in urls} | {vault.host_of(k) for k in self.config.hosts}
        hosts |= {m[0] for m in map(parse_mr_url, self.config.mr_links) if m}
        source = self.skill_source.text().strip()
        parsed = review_skill.parse_source(source) if source else None
        if parsed:
            hosts.add(parsed[0])
        self.access.setText("Checking…")

        def work():
            rows = [self._claude_check()]
            # Every GitLab server found, whatever its name (self-hosted ones too), each with its own token.
            servers = gitlab_hosts(hosts)
            for host in servers:
                for key in vault.keys_of_host(host):  # the host's token and its groups' tokens
                    if key == host and len(vault.keys_of_host(host)) > 1 and not vault.get_token(key):
                        continue  # group tokens only: no host-wide token is expected
                    rows += review_skill.check_token(key)
            if source:
                rows += review_skill.check_source(source)
            if not servers:
                rows.append(review_skill.Check(None, "No GitLab server found among your repositories yet"))
            return rows

        self.tasks.submit_api(work, self._on_access)

    @staticmethod
    def _claude_check():
        st = ai.status()
        if st.ready:
            return review_skill.Check(True, "Claude Code signed in")
        return review_skill.Check(False, st.detail, "Run `claude auth login`, or use Sign in in the Claude tab."
                                  if st.path else "Install it from https://claude.com/claude-code")

    def _on_access(self, rows, error):
        if error:
            self.access.setText(f"<span style='color:{C['red']}'>●</span> {error}")
            return
        html = []
        for r in rows:
            color = C["green"] if r.ok else C["orange"] if r.ok is None else C["red"]
            detail = f"<br><span style='color:{C['muted']}'>{r.detail}</span>" if r.detail else ""
            html.append(f"<span style='color:{color}'>●</span>&nbsp; {r.title}{detail}")
        self.access.setText("<br>".join(html))

    def _measure_cache(self):
        self.tasks.submit(ai_review.cache_usage, self._on_cache)

    def _on_cache(self, usage, error):
        if error or usage is None:
            self.cache_label.setText(f"Could not measure: {error}")
            return
        size, n = usage
        text = f"{size / 1e9:.2f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB"
        self.cache_label.setText(f"{text} in {n} project{'s' if n != 1 else ''}" if n else "Empty")
        self.cache_clean.setEnabled(bool(n or size))

    def _clean_cache(self):
        if QMessageBox.question(self, "Review clones", "Delete every review clone? They are downloaded again "
                                "at the next AI review of each project.") != QMessageBox.Yes:
            return
        self.cache_clean.setEnabled(False)
        self.cache_label.setText("Cleaning…")
        self.tasks.submit(ai_review.clear_cache, lambda _r, err: (
            err and QMessageBox.warning(self, "Review clones", f"Some files could not be deleted "
                                        f"(a review may be running):\n{err}"), self._measure_cache()))

    def _on_ai_status(self, st, error):
        if error or st is None:
            self.ai_state.setText(f"Claude Code: {error}")
            return
        if st.ready:
            self.ai_state.setText(f"<span style='color:{C['green']}'>●</span>  Claude Code signed in")
            self.ai_state.setToolTip(st.path)
        elif st.path:
            self.ai_state.setText(f"<span style='color:{C['orange']}'>●</span>  {st.detail}")
            self.ai_login.show()
        else:
            self.ai_state.setText(f"<span style='color:{C['red']}'>●</span>  Claude Code is not installed: "
                                  "<a href='https://claude.com/claude-code'>install it</a>, then sign in")
            self.ai_state.setOpenExternalLinks(True)

    def export_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export configuration", "gitenough-config.json",
                                              "JSON (*.json)")
        if not path:
            return
        data = {
            "gitenough": 1,
            "repos": self.current_urls(),
            "base_branches": [b.strip() for b in self.base_edit.text().split(",") if b.strip()],
            "auto_fetch_minutes": self.fetch_spin.value(),
            "hosts": {h: r.user.text().strip() for h, r in self.host_rows.items() if r.user.text().strip()},
        }
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError as exc:
            QMessageBox.critical(self, "Export", f"Could not write the file:\n{exc}")
            return
        QMessageBox.information(self, "Export", f"{len(data['repos'])} repositories exported to\n{path}")

    def import_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import configuration", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            repos = [str(u).strip() for u in data.get("repos", []) if str(u).strip()]
        except (OSError, ValueError, AttributeError) as exc:
            QMessageBox.critical(self, "Import", f"Not a GitEnough configuration file:\n{exc}")
            return
        current = self.current_urls()
        added = [u for u in repos if not any(same_repo(u, c) for c in current)]
        # Merged into the form only: nothing is saved until the user clicks Save.
        self.repos_edit.setPlainText("\n".join(current + added))
        if data.get("base_branches"):
            self.base_edit.setText(", ".join(data["base_branches"]))
        if isinstance(data.get("auto_fetch_minutes"), int):
            self.fetch_spin.setValue(data["auto_fetch_minutes"])
        for host, user in (data.get("hosts") or {}).items():
            self.config.hosts.setdefault(host, user)
        self.sync_hosts()
        for host, user in (data.get("hosts") or {}).items():
            row = self.host_rows.get(host)
            if row and not row.user.text().strip():
                row.user.setText(user)
        QMessageBox.information(self, "Import", f"{len(added)} new repositories added, "
                                f"{len(repos) - len(added)} already listed.\n\nClick Save to apply.")

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Root folder", self.root_edit.text())
        if path:
            self.root_edit.setText(path.replace("/", "\\"))

    def _repos_tab(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(QLabel("Repository URLs (one per line)"))
        self.repos_edit = QPlainTextEdit("\n".join(self.config.repos))
        self.repos_edit.setPlaceholderText("https://github.com/org/project.git\n"
                                           "https://gitlab.example.com/group/other.git\n"
                                           "git@github.com:org/project-ssh.git")
        lay.addWidget(self.repos_edit, 1)
        lay.addWidget(self._hint("Empty lines and lines starting with # are ignored. "
                                 "SSH URLs use your SSH keys; PATs only apply to HTTPS."))
        self.unhide = False
        if self.config.hidden:
            row = QHBoxLayout()
            label = self._hint(f"{len(self.config.hidden)} folder(s) hidden from the list.")
            show = QPushButton("Show them again")
            show.setObjectName("rowAction")
            show.clicked.connect(lambda: (setattr(self, "unhide", True), label.setText("Hidden folders will show "
                                          "again after Save."), show.setEnabled(False)))
            row.addWidget(label, 1)
            row.addWidget(show)
            lay.addLayout(row)
        return page

    def _access_tab(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(self._hint(
            "One token per git host. Tokens are stored in Windows Credential Manager "
            "(encrypted by your Windows session), never in the config file or the repositories. "
            "Several organizations on one server, each with its own account: add a token per group "
            "(e.g. gitlab.com/my-group). A repository uses its group's token when there is one, else its host's."))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        self.hosts_layout = QVBoxLayout(holder)
        self.hosts_layout.setContentsMargins(0, 0, 4, 0)
        self.hosts_layout.setSpacing(10)
        self.hosts_empty = self._hint("No HTTPS host detected. Add URLs in the Repositories tab.")
        self.hosts_layout.addWidget(self.hosts_empty)
        self.hosts_layout.addStretch()
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)

        add = QHBoxLayout()
        self.add_host_edit = QLineEdit()
        self.add_host_edit.setPlaceholderText("Add a host (git.example.com) or a group with its own account "
                                              "(gitlab.com/my-group)")
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_host)
        self.add_host_edit.returnPressed.connect(self._add_host)
        add.addWidget(self.add_host_edit, 1)
        add.addWidget(add_btn)
        lay.addLayout(add)
        return page

    def current_urls(self) -> list[str]:
        return parse_repo_lines(self.repos_edit.toPlainText())

    def _ensure_host(self, host: str):
        if not host or host in self.host_rows:
            return
        saved = vault.get_token(host) is not None
        row = HostRow(host, self.config.hosts.get(host, ""), saved, self)
        self.host_rows[host] = row
        self.hosts_layout.insertWidget(self.hosts_layout.count() - 1, row)
        self.hosts_empty.hide()

    def sync_hosts(self):
        hosts = {parse_url(u)[0] for u in self.current_urls() if is_http(u)} | set(self.config.hosts)
        for host in sorted(hosts):
            self._ensure_host(host)

    def _add_host(self):
        text = self.add_host_edit.text().strip().lower()
        if "//" in text:  # a pasted link: its host and top-level group
            host, parts = parse_url(text.split("/-/", 1)[0])
            text = f"{host}/{parts[0]}" if parts else host
        self._ensure_host(text.strip("/"))
        self.add_host_edit.clear()

    def accept(self):
        urls = self.current_urls()
        bad = [u for u in urls if not parse_url(u)[1]]
        if bad:
            QMessageBox.warning(self, "Invalid URL", "Unrecognized URL:\n" + "\n".join(bad))
            self.tabs.setCurrentIndex(1)
            return
        try:
            for host, row in self.host_rows.items():
                token = row.token.text().strip()
                if row.removed and not token:
                    vault.delete_token(host)
                    self.config.hosts.pop(host, None)
                    continue
                if token:
                    vault.set_token(host, token)
                if token or row.saved:
                    self.config.hosts[host] = row.user.text().strip()
        except Exception as exc:  # noqa: BLE001 - keyring backend errors vary
            QMessageBox.critical(self, "Error", f"Could not save the token:\n{exc}")
            return
        self.config.root = self.root_edit.text().strip()
        self.config.base_branches = [b.strip() for b in self.base_edit.text().split(",") if b.strip()]
        self.config.auto_fetch_minutes = self.fetch_spin.value()
        self.config.watch_files = self.watch_cb.isChecked()
        self.config.visual_studio = self.vs_combo.currentData()
        vault.set_scopes(self.config.hosts)
        self.config.ai_commit_model = self._model_of(self.commit_model)
        self.config.ai_review_model = self._model_of(self.review_model)
        self.config.repos = urls
        if self.unhide:
            self.config.hidden = []
        self.config.save()
        super().accept()
