"""A merge request's pipeline inside the review window: stages, jobs, and the log of the job clicked."""

import re

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
                               QVBoxLayout, QWidget)

from .diff_view import TEXT_BG, mono_font
from .style import C
from .widgets import ElidedLabel, Spinner

JOB = {  # status -> (icon, colour key)
    "success": ("✓", "green"), "failed": ("✕", "red"), "running": ("●", "blue"), "pending": ("●", "orange"),
    "created": ("○", "faint"), "waiting_for_resource": ("●", "orange"), "preparing": ("●", "orange"),
    "scheduled": ("◷", "orange"), "manual": ("▶", "muted"), "canceled": ("–", "faint"), "skipped": ("–", "faint"),
}
ACTIVE = {"running", "pending", "created", "waiting_for_resource", "preparing", "scheduled"}
SECTION_RE = re.compile(r"section_(?:start|end):\d+:[^\r\n]*?\r")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def chip_style(color_key: str) -> str:
    color = C[color_key]
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return (f"background: rgba({r},{g},{b},0.13); color: {color}; border: 1px solid rgba({r},{g},{b},0.30);"
            "border-radius: 8px; padding: 2px 9px; font-size: 8.5pt; font-weight: 600;")


def clean_log(text: str) -> str:
    """GitLab job logs carry ANSI colours, section markers and \\r progress rewrites."""
    text = ANSI_RE.sub("", SECTION_RE.sub("", text))
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        lines.append(line.rsplit("\r", 1)[-1])  # a progress line keeps only its last state
    return "\n".join(lines).rstrip()


def duration(seconds) -> str:
    if not seconds:
        return ""
    s = int(float(seconds))
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


class _LogHighlighter(QSyntaxHighlighter):
    """Commands and errors stand out in a job log (its own colours are stripped)."""

    def __init__(self, doc):
        super().__init__(doc)
        self.cmd, self.err, self.warn, self.ok = (QTextCharFormat() for _ in range(4))
        self.cmd.setForeground(QColor(C["blue"]))
        self.err.setForeground(QColor(C["red"]))
        self.warn.setForeground(QColor(C["orange"]))
        self.ok.setForeground(QColor(C["green"]))

    def highlightBlock(self, text: str):
        low = text.lower()
        if text.startswith("$ "):
            self.setFormat(0, len(text), self.cmd)
        elif "error" in low or "failed" in low or "failure" in low or "exception" in low:
            self.setFormat(0, len(text), self.err)
        elif "warning" in low:
            self.setFormat(0, len(text), self.warn)
        elif low.startswith("job succeeded") or "passed!" in low:
            self.setFormat(0, len(text), self.ok)


class JobCard(QFrame):
    """One job: coloured status icon, name, duration; click shows its log."""

    clicked = Signal(dict)

    def __init__(self, job: dict, selected: bool):
        super().__init__()
        self.job = job
        status = job.get("status", "")
        icon, color = JOB.get(status, ("●", "faint"))
        if status == "failed" and job.get("allow_failure"):
            icon, color = "!", "orange"
        self.setObjectName("jobCard")
        self.setCursor(Qt.PointingHandCursor)
        border = C["accent"] if selected else C["border"]
        self.setStyleSheet(f"QFrame#jobCard {{ background: {C['surface']}; border: 1px solid {border};"
                           f" border-radius: 8px; }} QFrame#jobCard:hover {{ border-color: {C[color]}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(10)
        mark = QLabel(icon)
        mark.setFixedWidth(14)
        mark.setAlignment(Qt.AlignCenter)
        mark.setStyleSheet(f"color: {C[color]}; font-weight: 800; font-size: 10.5pt; border: none;")
        text = QVBoxLayout()
        text.setSpacing(0)
        name = ElidedLabel(job.get("name", "") + ("  ↗" if job.get("bridge") else ""), Qt.ElideRight)
        name.setStyleSheet(f"color: {C['text']}; font-weight: 600; border: none;")
        extra = duration(job.get("duration")) or status.replace("_", " ")
        if job.get("allow_failure") and status == "failed":
            extra += " · allowed to fail"
        info = QLabel(extra)
        info.setStyleSheet(f"color: {C['faint']}; font-size: 8.5pt; border: none;")
        text.addWidget(name)
        text.addWidget(info)
        lay.addWidget(mark)
        lay.addLayout(text, 1)
        self.setToolTip(f"{job.get('name')}: {status.replace('_', ' ')}")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.job)


class PipelinePage(QWidget):
    def __init__(self, tasks, parent=None):
        super().__init__(parent)
        self.tasks = tasks
        self.client = None
        self.project = ""
        self.pipeline_id = 0
        self.url = ""
        self.jobs: list[dict] = []
        self.selected: dict | None = None
        self.loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QWidget()
        header.setObjectName("diffHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 10, 10, 10)
        hl.setSpacing(10)
        self.status = QLabel("")
        self.meta = ElidedLabel("", Qt.ElideRight)
        self.meta.setObjectName("muted")
        self.spinner = Spinner(14)
        self.spinner.hide()
        refresh = QPushButton("Refresh")
        refresh.setObjectName("rowAction")
        refresh.clicked.connect(self.reload)
        open_btn = QPushButton("Open pipeline in GitLab")
        open_btn.clicked.connect(lambda: self.url and QDesktopServices.openUrl(QUrl(self.url)))
        hl.addWidget(self.status)
        hl.addWidget(self.meta, 1)
        hl.addWidget(self.spinner)
        hl.addWidget(refresh)
        hl.addWidget(open_btn)
        lay.addWidget(header)

        split = QSplitter(Qt.Vertical)
        split.setHandleWidth(1)
        self.stages_area = QScrollArea()
        self.stages_area.setObjectName("plainScroll")
        self.stages_area.setWidgetResizable(True)
        self.stages_holder = QWidget()
        self.stages_holder.setObjectName("stages")
        self.stages_holder.setStyleSheet(f"QWidget#stages {{ background: {C['bg']}; }}")
        self.stages_lay = QHBoxLayout(self.stages_holder)
        self.stages_lay.setContentsMargins(16, 14, 16, 14)
        self.stages_lay.setSpacing(14)
        self.stages_lay.addStretch()
        self.stages_area.setWidget(self.stages_holder)
        split.addWidget(self.stages_area)

        log_panel = QWidget()
        ll = QVBoxLayout(log_panel)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        log_head = QWidget()
        log_head.setObjectName("diffHeader")
        lh = QHBoxLayout(log_head)
        lh.setContentsMargins(14, 6, 10, 6)
        self.log_title = ElidedLabel("Click a job to see its log", Qt.ElideRight)
        self.log_title.setObjectName("muted")
        self.job_btn = QPushButton("Open job in GitLab")
        self.job_btn.setObjectName("rowAction")
        self.job_btn.clicked.connect(lambda: self.selected and QDesktopServices.openUrl(
            QUrl(self.selected.get("web_url", ""))))
        self.job_btn.hide()
        lh.addWidget(self.log_title, 1)
        lh.addWidget(self.job_btn)
        ll.addWidget(log_head)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono_font())
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log.setStyleSheet(f"QPlainTextEdit {{ background: {TEXT_BG}; border: none; }}")
        self.log_colors = _LogHighlighter(self.log.document())
        ll.addWidget(self.log, 1)
        split.addWidget(log_panel)
        split.setSizes([260, 420])
        lay.addWidget(split, 1)

    @property
    def active(self) -> bool:
        return any(j.get("status") in ACTIVE for j in self.jobs)

    # ---------- data ----------
    def show_pipeline(self, client, project: str, pipeline_id: int, url: str):
        changed = (project, pipeline_id) != (self.project, self.pipeline_id)
        self.client, self.project, self.pipeline_id, self.url = client, project, pipeline_id, url
        if changed:
            self.jobs, self.selected = [], None
            self.log.clear()
            self.log_title.setText("Click a job to see its log")
            self.job_btn.hide()
            self._render_jobs()
        self.reload()

    def reload(self):
        if self.loading or self.client is None or not self.pipeline_id:
            return
        self.loading = True
        self.spinner.show()
        client, project, pid = self.client, self.project, self.pipeline_id

        def work():
            return client.pipeline(project, pid), client.pipeline_jobs(project, pid)

        self.tasks.submit_api(work, self._on_loaded)

    def _on_loaded(self, result, error):
        self.loading = False
        self.spinner.hide()
        if error:
            self.meta.setText(str(error))
            return
        pipeline, jobs = result
        self.jobs = jobs
        status = pipeline.get("status", "")
        icon, color = JOB.get(status, ("●", "faint"))
        self.status.setText(f"{icon}  Pipeline {status.replace('_', ' ')}")
        self.status.setStyleSheet(chip_style(color))
        bits = [f"#{pipeline.get('id', '')}", pipeline.get("ref", "")]
        if pipeline.get("duration"):
            bits.append(duration(pipeline["duration"]))
        failed = sum(1 for j in jobs if j.get("status") == "failed" and not j.get("allow_failure"))
        bits.append(f"{len(jobs)} jobs" + (f", {failed} failed" if failed else ""))
        self.meta.setText("  ·  ".join(b for b in bits if b))
        self._render_jobs()
        if self.selected is not None:
            fresh = next((j for j in jobs if j.get("id") == self.selected.get("id")), None)
            if fresh is not None and (fresh.get("status") in ACTIVE or fresh.get("status") !=
                                      self.selected.get("status")):
                self.select(fresh)  # its log is still growing (or it just finished)
        elif jobs:
            first = next((j for j in jobs if j.get("status") == "failed" and not j.get("allow_failure")), None)
            if first is not None:
                self.select(first)  # a failed job is what you came for

    def _render_jobs(self):
        lay = self.stages_lay
        while lay.count() > 1:
            w = lay.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        stages: dict[str, list[dict]] = {}
        # Stage order: GitLab creates the jobs stage by stage, so the lowest job id tells.
        for job in sorted(self.jobs, key=lambda j: j.get("id") or 0):
            stages.setdefault(job.get("stage") or "jobs", []).append(job)
        for stage, jobs in stages.items():
            column = QFrame()
            column.setObjectName("stageColumn")
            column.setMinimumWidth(210)
            cl = QVBoxLayout(column)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(6)
            title = QLabel(f"{stage.upper()}  <span style='color:{C['faint']}'>{len(jobs)}</span>")
            title.setTextFormat(Qt.RichText)
            title.setStyleSheet(f"color: {C['muted']}; font-size: 8.5pt; font-weight: 700; letter-spacing: 1px;")
            cl.addWidget(title)
            for job in sorted(jobs, key=lambda j: j.get("name", "")):
                cl.addWidget(self._job_button(job))
            cl.addStretch()
            lay.insertWidget(lay.count() - 1, column)

    def _job_button(self, job: dict) -> JobCard:
        card = JobCard(job, self.selected is not None and self.selected.get("id") == job.get("id"))
        card.clicked.connect(self.select)
        return card

    def select(self, job: dict):
        self.selected = job
        self._render_jobs()
        status = job.get("status", "")
        self.log_title.setText(f"{job.get('name')}  ·  {status.replace('_', ' ')}"
                               + (f"  ·  {duration(job.get('duration'))}" if job.get("duration") else ""))
        self.job_btn.setVisible(bool(job.get("web_url")))
        if job.get("bridge"):
            self.log.setPlainText("This job triggers a child or downstream pipeline: open it in GitLab.")
            return
        client, project, job_id = self.client, self.project, job.get("id")
        at_end = self.log.verticalScrollBar().value() >= self.log.verticalScrollBar().maximum() - 4

        def done(text, error):
            if self.selected is None or self.selected.get("id") != job_id:
                return  # another job was clicked meanwhile
            self.log.setPlainText(str(error) if error else (clean_log(text) or "(empty log)"))
            if at_end or not error:
                self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

        self.tasks.submit_api(lambda: client.job_log(project, job_id), done)
