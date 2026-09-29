import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from . import vault
from .about import AboutPage
from .config import Config
from .git_ops import Credential, GitError, is_http, parse_url, same_repo, test_access
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
        url = next((u for u in self.dialog.current_urls()
                    if is_http(u) and parse_url(u)[0] == self.host), None)
        if not url:
            self.show_result(False, "No HTTPS repository from this host in the list.")
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
            "(encrypted by your Windows session), never in the config file or the repositories."))
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
        self.add_host_edit.setPlaceholderText("Add a host manually (e.g. git.example.com)")
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
        host = self.add_host_edit.text().strip().lower()
        if "//" in host:
            host = parse_url(host)[0]
        self._ensure_host(host.strip("/"))
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
        self.config.repos = urls
        if self.unhide:
            self.config.hidden = []
        self.config.save()
        super().accept()
