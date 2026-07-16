"""
Watches the music folder for changes made outside the app (Explorer/Finder/
adb/sftp — anything that isn't the sync or the API) and invalidates the
track cache so the library picks them up without a manual rescan.

Uses the `watchdog` package. If it isn't installed, start() just returns
False — this is a nice-to-have, not a hard dependency, so the app still
runs fine without it (falls back to the existing pull-to-refresh /
periodic cache TTL behaviour).
"""
import threading
import time
from .config import cfg
from .tracks import invalidate_cache

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG = True
except ImportError:
    WATCHDOG = False
    Observer = None
    FileSystemEventHandler = object

_observer = None
_lock = threading.Lock()

WATCHED_EXTS = (".mp3", ".mp4", ".webm", ".mkv", ".lrc", ".txt", ".jpg", ".jpeg", ".png")


class _Handler(FileSystemEventHandler):
    def __init__(self):
        self._last = 0
        self._debounce_lock = threading.Lock()

    def _maybe_invalidate(self, path: str):
        if not str(path).lower().endswith(WATCHED_EXTS):
            return
        # Debounce: yt-dlp/reindex themselves touch many files in a burst;
        # one invalidate per ~1s window is plenty, no need to thrash.
        with self._debounce_lock:
            now = time.time()
            if now - self._last < 1.0:
                return
            self._last = now
        invalidate_cache()

    def on_created(self, event):  self._maybe_invalidate(event.src_path)
    def on_deleted(self, event):  self._maybe_invalidate(event.src_path)
    def on_moved(self, event):    self._maybe_invalidate(event.dest_path)
    def on_modified(self, event): pass  # metadata-only writes (ID3 tagging) don't need a rescan


def start():
    """Idempotent — safe to call more than once (e.g. after music_dir changes)."""
    global _observer
    if not WATCHDOG:
        return False
    if not cfg.load_settings().get("watch_library", True):
        return False
    with _lock:
        stop()
        try:
            obs = Observer()
            obs.schedule(_Handler(), str(cfg.music_dir), recursive=True)
            obs.start()
            _observer = obs
            return True
        except Exception:
            _observer = None
            return False


def stop():
    global _observer
    if _observer:
        try:
            _observer.stop()
            _observer.join(timeout=2)
        except Exception:
            pass
        _observer = None


def is_running() -> bool:
    return _observer is not None and getattr(_observer, "is_alive", lambda: False)()
