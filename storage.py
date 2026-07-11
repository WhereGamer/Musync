"""
Atomic JSON storage with per-file RW locks and thread-safe cache.

All reads/writes go through load_json / save_json which:
  • Hold a per-file RLock (re-entrant, so the same thread can read
    while holding a write in a nested call).
  • Write atomically via a .tmp file + replace() so a crash or
    concurrent read never sees a half-written file.
  • Maintain an in-process read cache so parallel download threads
    don't hammer the same JSON file on every stat_inc call.
"""
import json
import threading
from pathlib import Path
from typing import Any

# ── Per-file RLocks ────────────────────────────────────────
_locks: dict[str, threading.RLock] = {}
_lock_meta = threading.Lock()

def _get_lock(path: str) -> threading.RLock:
    with _lock_meta:
        if path not in _locks:
            _locks[path] = threading.RLock()
        return _locks[path]

# ── In-process read cache ──────────────────────────────────
_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()

def _invalidate_cache(path: str):
    with _cache_lock:
        _cache.pop(path, None)

def load_json(path, default):
    key = str(path)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    with _get_lock(key):
        try:
            data = json.loads(Path(path).read_text("utf-8"))
        except Exception:
            data = default
        with _cache_lock:
            _cache[key] = data
        return data

def save_json(path: Path, data):
    path = Path(path)
    key  = str(path)
    tmp  = path.with_suffix(".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    with _get_lock(key):
        tmp.write_text(text, "utf-8")
        tmp.replace(path)
        # Update cache immediately so the next load_json in the same
        # process sees the new value without a disk read.
        with _cache_lock:
            _cache[key] = data

def invalidate_json_cache(path=None):
    """Evict one file or the whole cache from the read cache."""
    with _cache_lock:
        if path is None:
            _cache.clear()
        else:
            _cache.pop(str(path), None)

# ── Safe path resolution ───────────────────────────────────
def safe_path(base: Path, user_input: str) -> Path | None:
    """
    Resolve a user-supplied relative path and ensure it stays strictly
    inside `base`. Returns None on any traversal attempt or error.
    Used in every Flask route that accepts a filename/path parameter.
    """
    if not user_input:
        return None
    # Strip leading slashes / dots so "../etc/passwd" can't escape
    sanitized = user_input.lstrip("/\\")
    try:
        resolved   = (base / sanitized).resolve()
        base_resolved = base.resolve()
        if not str(resolved).startswith(str(base_resolved) + "/") \
                and resolved != base_resolved:
            return None
        return resolved
    except Exception:
        return None
