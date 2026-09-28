"""Top-right notification offering the new version, with a one-click self-update."""

import threading

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from . import __version__, updater
from .style import C


class UpdateToast(QFrame):
    def __init__(self, main, release: updater.Release):
        super().__init__(main)
        self.main, self.release = main, release
        self._progress = (0, 0)
        self._lock = threading.Lock()
        self.setObjectName("toast")
        self.setFixedWidth(380)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 12, 14)
        lay.setSpacing(6)
        head = QHBoxLayout()
        title = QLabel("Update available")
        title.setStyleSheet("font-weight: 700; font-size: 11pt;")
        close = QPushButton("×")
        close.setObjectName("ghost")
        close.setFixedWidth(26)
        close.clicked.connect(self.hide)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(close)
        lay.addLayout(head)
        self.text = QLabel(f"GitEnough <b>{release.version}</b> is out (you have {__version__})."
                           + (f"<br><span style='color:{C['muted']}'>{release.notes}</span>" if release.notes else ""))
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.RichText)
        lay.addWidget(self.text)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.hide()
        lay.addWidget(self.bar)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.later = QPushButton("Later")
        self.later.clicked.connect(self.hide)
        self.update_btn = QPushButton("Update now" if updater.running_exe() else "Open download page")
        self.update_btn.setObjectName("primary")
        self.update_btn.clicked.connect(self.start)
        buttons.addWidget(self.later)
        buttons.addWidget(self.update_btn)
        lay.addLayout(buttons)
        self.poll = QTimer(self)
        self.poll.setInterval(150)
        self.poll.timeout.connect(self._show_progress)
        self.adjustSize()
        self.place()

    def place(self):
        parent = self.parentWidget()
        self.move(parent.width() - self.width() - 24, 70)
        self.raise_()

    def start(self):
        exe = updater.running_exe()
        if not exe:
            QDesktopServices.openUrl(QUrl(updater.REPO_URL))
            self.hide()
            return
        self.update_btn.setEnabled(False)
        self.later.setEnabled(False)
        self.text.setText(f"Downloading GitEnough {self.release.version}…")
        self.bar.show()
        self.poll.start()

        def progress(done, total):
            with self._lock:  # Called on the download thread.
                self._progress = (done, total)

        self.main.tasks.submit_network(updater.download, lambda path, err: self._downloaded(exe, path, err),
                                       self.release, progress)

    def _show_progress(self):
        with self._lock:
            done, total = self._progress
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        else:
            self.bar.setRange(0, 0)

    def _downloaded(self, exe: str, path: str, error):
        self.poll.stop()
        if error:
            self.text.setText(f"<span style='color:{C['red']}'>Update failed: {error}</span>")
            self.update_btn.setText("Open download page")
            self.update_btn.setEnabled(True)
            self.update_btn.clicked.disconnect()
            self.update_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(updater.REPO_URL)))
            self.later.setEnabled(True)
            return
        try:
            updater.install(exe, path)
            updater.relaunch(exe)
        except OSError as exc:
            self.text.setText(f"<span style='color:{C['red']}'>Could not replace the executable: {exc}</span>")
            self.later.setEnabled(True)
            return
        self.text.setText("Restarting on the new version…")
        QTimer.singleShot(300, self.main.close)
