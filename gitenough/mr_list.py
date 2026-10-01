"""Merge requests waiting for the user on GitLab (reviewer, assignee, mentioned, own), plus pasted links."""

import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import gitlab, vault
from .git_ops import GitError, parse_url
from .mr_review import ReviewWindow, when
from .style import C
from .widgets import ElidedLabel, Spinner, icon_button, keep_size

REASONS = {"reviewer": ("Review", C["violet"]), "assignee": ("Assigned", C["blue"]),
           "mentioned": ("Mentioned", C["orange"]), "author": ("Mine", C["green"]), "added": ("Added", C["muted"])}
FILTERS = [("all", "All"), ("reviewer", "To review"), ("assignee", "Assigned"), ("mentioned", "Mentioned"),
           ("author", "Mine"), ("added", "Added")]


def candidate_hosts(config, repo_urls: list[str]) -> set[str]:
    """HTTPS hosts that may be GitLab servers: tracked repositories, pasted links, the review skill source."""
    hosts = {parse_url(u)[0] for u in [*config.repos, *repo_urls] if u.lower().startswith("http")}
    hosts |= {vault.host_of(k) for k in config.hosts}  # group token keys count for their server
    hosts |= {m[0] for m in map(gitlab.parse_mr_url, config.mr_links) if m}
    return {h for h in hosts if h}


def load(config, candidates: set[str]):
    """(merge requests, errors, GitLab hosts that have no token yet)."""
    found: dict[str, gitlab.MergeRequest] = {}
    errors = []
    servers = gitlab.gitlab_hosts(candidates)
    keys = {h: [k for k in vault.keys_of_host(h) if vault.get_token(k)] for h in servers}
    missing = [h for h in servers if not keys[h]]
    links = [(url, parsed) for url in config.mr_links if (parsed := gitlab.parse_mr_url(url))]

    def mine(key):
        return gitlab.Client.for_key(key).my_merge_requests()

    def linked(parsed):
        host, project, iid = parsed
        return gitlab.Client.for_project(host, project).merge_request(project, iid)

    # One pass per token (organizations on the same server may each have their own account) and one
    # request per pasted link, all at once: each is a round trip to a server.
    with ThreadPoolExecutor(max_workers=8) as pool:
        lists = {key: pool.submit(mine, key) for h in servers for key in keys[h]}
        singles = {url: pool.submit(linked, parsed) for url, parsed in links}
        for key, future in lists.items():
            try:
                for mr in future.result():
                    if mr.key in found:
                        found[mr.key].reasons |= mr.reasons
                    else:
                        found[mr.key] = mr
            except GitError as exc:
                errors.append(f"{key}: {exc}")
        for url, parsed in links:
            key = gitlab.mr_key(*parsed)
            if key in found:
                found[key].reasons.add("added")
                continue
            try:
                mr = singles[url].result()
            except GitError as exc:
                errors.append(f"{url}: {exc}")
                mr = gitlab.MergeRequest(*parsed, title="(could not be loaded)", web_url=url)
            mr.reasons.add("added")
            found[mr.key] = mr
    return sorted(found.values(), key=lambda m: m.updated, reverse=True), errors, missing, servers


class MergeRequestsWindow(QWidget):
    def __init__(self, main):
        super().__init__(main, Qt.Window)
        self.main, self.config, self.tasks = main, main.config, main.tasks
        self.mrs: list[gitlab.MergeRequest] = []
        self.reviews: dict[str, ReviewWindow] = {}
        self.loading = False
        self.last_load = 0.0
        self.filter = "all"
        self.setWindowTitle("Merge requests")
        self.resize(1200, 720)
        keep_size(self, self.config, "merge_requests")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("windowHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 14, 16, 14)
        title = QLabel("Merge requests")
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        hl.addWidget(title)
        self.loading_bar = Spinner(16)  # while the lists load
        self.loading_bar.hide()
        hl.addWidget(self.loading_bar)
        self.status = ElidedLabel("", Qt.ElideRight)
        self.status.setObjectName("muted")
        hl.addWidget(self.status, 1)
        self.link = QLineEdit()
        self.link.setPlaceholderText("Paste a merge request link to add it")
        self.link.setMinimumWidth(360)
        self.link.returnPressed.connect(self.add_link)
        add = QPushButton("Add")
        add.clicked.connect(self.add_link)
        refresh = icon_button("refresh", "Reload (F5)")
        refresh.clicked.connect(self.reload)
        for w in (self.link, add, refresh):
            hl.addWidget(w)
        root.addWidget(header)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(10)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.group = QButtonGroup(self)
        self.chip_buttons = {}
        for key, label in FILTERS:
            b = QPushButton(label)
            b.setObjectName("filterChip")
            b.setCheckable(True)
            b.setChecked(key == "all")
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            self.group.addButton(b)
            self.chip_buttons[key] = b
            chips.addWidget(b)
        chips.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(220)
        self.search.textChanged.connect(self.populate)
        chips.addWidget(self.search)
        bl.addLayout(chips)

        self.banner = QWidget()
        bnl = QHBoxLayout(self.banner)
        bnl.setContentsMargins(0, 0, 0, 0)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        open_settings = QPushButton("Open Settings")
        open_settings.clicked.connect(self.main.open_settings)
        bnl.addWidget(self.banner_text, 1)
        bnl.addWidget(open_settings)
        self.banner.hide()
        bl.addWidget(self.banner)

        self.table = QTreeWidget()
        self.table.setObjectName("mrTable")
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(False)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setHeaderLabels(["Merge request", "Project", "Author", "Why", "Updated", "Comments"])
        self.table.setColumnWidth(0, 460)
        self.table.setColumnWidth(1, 240)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 170)
        self.table.setColumnWidth(4, 90)
        self.table.itemActivated.connect(lambda item, _c: self.open_item(item))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        bl.addWidget(self.table, 1)
        hint = QLabel("Double-click or Enter to open · right-click for more")
        hint.setObjectName("muted")
        bl.addWidget(hint)
        root.addWidget(body, 1)
        self.reload()

    # ---------- data ----------
    def reload(self):
        if self.loading:
            return
        hosts = candidate_hosts(self.config, [p.url for p in getattr(self.main, "projects", [])])
        self.loading = True
        self.last_load = time.monotonic()
        self.status.setText("Loading merge requests…")
        self.loading_bar.show()
        self.tasks.submit_api(load, self._on_load, self.config, hosts)

    def _on_load(self, result, error):
        self.loading = False
        self.loading_bar.hide()
        if error:
            self.status.setText(str(error))
            return
        self.mrs, errors, missing, servers = result
        if missing or not servers:
            which = ", ".join(missing) if missing else "your GitLab server"
            self.banner_text.setText(
                f"No access token for {which}. Each GitLab server needs its own personal access token with the "
                "“api” scope: add it in Settings > Access (PAT), then reload.")
            self.banner.show()
        else:
            self.banner.hide()
        self.status.setText(errors[0] if errors else f"{len(self.mrs)} open merge request(s) on "
                            f"{', '.join(h for h in servers if h not in missing) or 'no server'}")
        self.populate()

    def set_filter(self, key: str):
        self.filter = key
        self.populate()

    def populate(self):
        term = self.search.text().strip().lower()
        current = self.table.currentItem().data(0, Qt.UserRole) if self.table.currentItem() else None
        scroll = self.table.verticalScrollBar().value()
        self.table.clear()
        counts = {k: 0 for k, _ in FILTERS}
        for mr in self.mrs:
            counts["all"] += 1
            for r in mr.reasons:
                counts[r] = counts.get(r, 0) + 1
            if self.filter != "all" and self.filter not in mr.reasons:
                continue
            if term and term not in f"{mr.title} {mr.project} {mr.author} !{mr.iid}".lower():
                continue
            why = ", ".join(REASONS[r][0] for r in ("reviewer", "assignee", "mentioned", "author", "added")
                            if r in mr.reasons)
            flags = (" · Draft" if mr.draft else "") + (" · Conflicts" if mr.conflicts else "")
            item = QTreeWidgetItem([f"!{mr.iid}  {mr.title}{flags}", mr.project, mr.author, why, when(mr.updated),
                                    str(mr.notes or "")])
            item.setData(0, Qt.UserRole, mr.key)
            item.setToolTip(0, mr.web_url)
            first = next((r for r in ("reviewer", "mentioned", "assignee", "author", "added") if r in mr.reasons), "")
            if first:
                item.setForeground(3, QColor(REASONS[first][1]))
            self.table.addTopLevelItem(item)
            if mr.key == current:
                self.table.setCurrentItem(item)
        for key, label in FILTERS:
            n = counts.get(key, 0)
            self.chip_buttons[key].setText(f"{label}  {n}" if n else label)
        self.table.verticalScrollBar().setValue(scroll)

    def add_link(self):
        url = self.link.text().strip()
        parsed = gitlab.parse_mr_url(url)
        if not parsed:
            QMessageBox.warning(self, "Add merge request",
                                "Not a merge request link. Expected something like\n"
                                "https://gitlab.com/group/project/-/merge_requests/123")
            return
        host, project, iid = parsed
        if not vault.token_for(host, project)[1]:
            QMessageBox.warning(self, "Add merge request",
                                f"No access token for {host}. Add one with the “api” scope in Settings > Access "
                                "(PAT) first.")
            return
        clean = f"https://{host}/{project}/-/merge_requests/{iid}"
        if not any(gitlab.parse_mr_url(u) == parsed for u in self.config.mr_links):
            self.config.mr_links.append(clean)
            self.config.save()
        self.link.clear()
        self.reload()
        key = gitlab.mr_key(host, project, iid)
        self.open_mr(next((m for m in self.mrs if m.key == key), None) or gitlab.MergeRequest(host, project, iid))

    # ---------- actions ----------
    def _mr_of(self, item) -> gitlab.MergeRequest | None:
        key = item.data(0, Qt.UserRole) if item else None
        return next((m for m in self.mrs if m.key == key), None)

    def open_item(self, item):
        mr = self._mr_of(item)
        if mr:
            self.open_mr(mr)

    def open_mr(self, mr: gitlab.MergeRequest):
        win = self.reviews.get(mr.key)
        if win is None:
            # Kept alive when closed: an AI review or a send may still report back to it.
            win = ReviewWindow(self.main, mr)
            self.reviews[mr.key] = win
        elif not win.isVisible():
            win.load()
        win.show()
        win.raise_()
        win.activateWindow()

    def context_menu(self, pos):
        item = self.table.itemAt(pos)
        mr = self._mr_of(item)
        if mr is None:
            return
        menu = QMenu(self)
        for text, fn, enabled in (
                ("Open review", lambda: self.open_mr(mr), True),
                ("Open in GitLab", lambda: QDesktopServices.openUrl(QUrl(mr.web_url)), bool(mr.web_url)),
                ("Remove from the list", lambda: self.remove_link(mr), "added" in mr.reasons)):
            act = QAction(text, menu)
            act.setEnabled(enabled)
            act.triggered.connect(fn)
            menu.addAction(act)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def remove_link(self, mr: gitlab.MergeRequest):
        key = (mr.host, mr.project, mr.iid)
        self.config.mr_links = [u for u in self.config.mr_links if gitlab.parse_mr_url(u) != key]
        self.config.save()
        mr.reasons.discard("added")
        if not mr.reasons:
            self.mrs = [m for m in self.mrs if m is not mr]
        self.populate()

    def changeEvent(self, event):
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow() and not self.loading
                and time.monotonic() - self.last_load > 120):
            self.reload()
        super().changeEvent(event)
