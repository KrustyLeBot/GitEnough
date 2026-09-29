import os
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


class _Bridge(QObject):
    done = Signal(object, object)


class Tasks:
    """Runs blocking work in thread pools and delivers results on the Qt main thread.

    Three pools, so slow work never delays what the user is looking at (diffs, file lists, history):
    network work (clone, fetch, pull, push) can take minutes per repository, and status scans of every
    repository (startup, focus, file watching) come in bursts of dozens.
    """

    def __init__(self, ui_workers: int = 4, network_workers: int = 6, status_workers: int = 0):
        # git status is mostly process start-up and disk reads: scale with the machine, within reason.
        status_workers = status_workers or max(4, min(10, (os.cpu_count() or 4) // 2))
        self.ui = ThreadPoolExecutor(max_workers=ui_workers, thread_name_prefix="ui")
        self.network = ThreadPoolExecutor(max_workers=network_workers, thread_name_prefix="net")
        self.status = ThreadPoolExecutor(max_workers=status_workers, thread_name_prefix="status")
        # Forge API calls and Claude runs get their own pools: a Pull all or an auto-fetch of every
        # repository must not leave a merge request or a commit message suggestion waiting behind it.
        self.api = ThreadPoolExecutor(max_workers=6, thread_name_prefix="api")
        self.ai = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ai")
        self._closed = False
        self._bridge = _Bridge()
        self._bridge.done.connect(self._deliver)

    def submit(self, fn: Callable[..., Any], callback: Callable[[Any, Exception | None], None],
               *args: Any) -> None:
        """Interactive work: local git reads the user is waiting for."""
        self._submit(self.ui, fn, callback, args)

    def submit_network(self, fn: Callable[..., Any], callback: Callable[[Any, Exception | None], None],
                       *args: Any) -> None:
        """Slow work that talks to a remote."""
        self._submit(self.network, fn, callback, args)

    def submit_api(self, fn: Callable[..., Any], callback: Callable[[Any, Exception | None], None],
                   *args: Any) -> None:
        """GitLab / forge REST calls the user is looking at."""
        self._submit(self.api, fn, callback, args)

    def submit_ai(self, fn: Callable[..., Any], callback: Callable[[Any, Exception | None], None],
                  *args: Any) -> None:
        """Claude Code runs (seconds to minutes each)."""
        self._submit(self.ai, fn, callback, args)

    def submit_status(self, fn: Callable[..., Any], callback: Callable[[Any, Exception | None], None],
                      *args: Any) -> None:
        """Local status scans of many repositories at once."""
        self._submit(self.status, fn, callback, args)

    def _submit(self, pool: ThreadPoolExecutor, fn, callback, args) -> None:
        if self._closed:
            return  # Window events can still fire while the app is closing.
        future = pool.submit(fn, *args)
        # Emitted from the worker thread; Qt queues it to the bridge's (main) thread.
        future.add_done_callback(lambda f: self._bridge.done.emit(callback, f))

    @staticmethod
    def _deliver(callback: Callable, future: Future) -> None:
        try:
            result, error = future.result(), None
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            result, error = None, exc
        callback(result, error)

    def shutdown(self) -> None:
        self._closed = True
        for pool in (self.ui, self.network, self.status, self.api, self.ai):
            pool.shutdown(wait=False, cancel_futures=True)
