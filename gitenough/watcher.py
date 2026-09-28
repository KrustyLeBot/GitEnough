"""Recursive file watching of the repositories, debounced into one refresh per repository.

Uses ReadDirectoryChangesW directly: one thread per repository sleeps in the kernel and wakes up with a
whole batch of changes at once. A build writing thousands of files costs a few wake-ups, not one Python
object per file event.
"""

import ctypes
import os
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QObject, QTimer, Signal

QUIET_DELAY = 0.8  # seconds without events before refreshing
MIN_INTERVAL = 2.5  # never refresh the same repository more often than this
# Changes that never affect what GitEnough shows: git's object store, logs and lock files, IDE caches.
# Prefixes also match the folder itself: creating a file there reports the folder as modified too.
NOISE_PREFIXES = (".git\\objects", ".git\\logs", ".git\\lfs", ".git\\fetch_head", ".git\\index.lock",
                  ".git\\gitk.cache", ".vs", ".idea", "node_modules")
NOISE_PARTS = ("\\.vs", "\\.idea", "\\node_modules")


def is_noise(name: str) -> bool:
    name = name.lower()
    # Only git's own lock files: yarn.lock, Cargo.lock... are real project files.
    git_lock = name.startswith(".git\\") and name.endswith(".lock")
    return (name == ".git" or git_lock or name.startswith(NOISE_PREFIXES)
            or any(part in name for part in NOISE_PARTS))

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.ReadDirectoryChangesW.restype = wintypes.BOOL
_k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
                                       wintypes.LPVOID]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.OpenThread.restype = wintypes.HANDLE
_k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]

FILE_LIST_DIRECTORY = 0x0001
SHARE_ALL = 0x7
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
NOTIFY_FILTER = 0x1 | 0x2 | 0x8 | 0x10  # file name, dir name, size, last write
THREAD_TERMINATE = 0x0001
INVALID_HANDLE = wintypes.HANDLE(-1).value
BUFFER_SIZE = 64 * 1024


class _DirWatch(threading.Thread):
    def __init__(self, path: str, touch):
        super().__init__(daemon=True, name=f"watch:{os.path.basename(path)}")
        self.path, self.touch = path, touch
        self.stopped = False
        self.handle = _k32.CreateFileW(path, FILE_LIST_DIRECTORY, SHARE_ALL, None, OPEN_EXISTING,
                                       FILE_FLAG_BACKUP_SEMANTICS, None)
        if not self.handle or self.handle == INVALID_HANDLE:
            raise OSError(f"cannot watch {path}")

    def run(self):
        buf = ctypes.create_string_buffer(BUFFER_SIZE)
        returned = wintypes.DWORD()
        while not self.stopped:
            ok = _k32.ReadDirectoryChangesW(self.handle, buf, BUFFER_SIZE, True, NOTIFY_FILTER,
                                            ctypes.byref(returned), None, None)
            if self.stopped or not ok:
                break
            if returned.value == 0 or self._relevant(buf.raw, returned.value):
                # 0 bytes = the kernel buffer overflowed: something changed, details unknown.
                self.touch(self.path)
        _k32.CloseHandle(self.handle)

    @staticmethod
    def _relevant(raw: bytes, size: int) -> bool:
        """True as soon as one entry of the batch is outside the noise list."""
        offset = 0
        while offset < size:
            # FILE_NOTIFY_INFORMATION: NextEntryOffset, Action, FileNameLength (DWORDs), then the UTF-16 name.
            next_entry = int.from_bytes(raw[offset:offset + 4], "little")
            length = int.from_bytes(raw[offset + 8:offset + 12], "little")
            name = raw[offset + 12:offset + 12 + length].decode("utf-16-le", errors="replace")
            if not is_noise(name):
                return True
            if not next_entry:
                return False
            offset += next_entry
        return False

    def stop(self):
        self.stopped = True
        # The thread is blocked in a synchronous ReadDirectoryChangesW: cancel it from here.
        th = _k32.OpenThread(THREAD_TERMINATE, False, self.native_id or 0)
        if th:
            _k32.CancelSynchronousIo(th)
            _k32.CloseHandle(th)


class RepoWatcher(QObject):
    changed = Signal(str)  # repository path, on the main thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self.watches: dict[str, _DirWatch] = {}
        self._lock = threading.Lock()
        self.pending: dict[str, float] = {}
        self.last: dict[str, float] = {}
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self._flush)
        self.timer.start()

    def touch(self, path: str):
        # Called from watch threads: only a timestamp, the main thread polls it.
        with self._lock:
            self.pending[path] = time.monotonic()

    def set_paths(self, paths: list[str]):
        wanted = {os.path.normpath(p) for p in paths if os.path.isdir(p)}
        for path in list(self.watches):
            if path not in wanted:
                self.watches.pop(path).stop()
        for path in wanted - set(self.watches):
            try:
                watch = _DirWatch(path, self.touch)
                watch.start()
                self.watches[path] = watch
            except OSError:
                pass  # Unwatchable folder (network drive, permissions): focus refresh still covers it.

    def _flush(self):
        now = time.monotonic()
        with self._lock:
            ready = [p for p, stamp in self.pending.items()
                     if now - stamp >= QUIET_DELAY and now - self.last.get(p, 0) >= MIN_INTERVAL]
            for path in ready:
                del self.pending[path]
                self.last[path] = now
        for path in ready:
            self.changed.emit(path)

    def stop(self):
        self.timer.stop()
        for watch in self.watches.values():
            watch.stop()
        self.watches.clear()
