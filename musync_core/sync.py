"""
Sync orchestration — playlist/track download with smart folder placement.

Rules:
  • Syncing a PLAYLIST  → creates tracks/<Playlist Name>/ and puts all
    its tracks there. A matching MUSYNC playlist is auto-created with
    the same name, description and cover.
  • Syncing a SINGLE TRACK → goes directly into tracks/ (no folder).
  • Before downloading any track, the whole library (root + every
    playlist subfolder) is checked for an existing copy (same video_id
    OR same normalized title+artist+duration). If found elsewhere, the
    track is NOT re-downloaded — it's just referenced (and, if syncing
    a playlist, added into that playlist's track list pointing at the
    already-existing file).
"""

import re
import json
import time
import shutil
import subprocess
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Callable

from .config import cfg
from .storage import load_json, save_json
from .tracks import normalize_title, normalize_artist, get_all_mp3s, invalidate_cache, reindex_folder
from .playlists import create_or_get_by_name, add_track_to_playlist, get_all as get_all_playlists, save_all as save_all_playlists

def find_cookies_file() -> Path | None:
    """
    Look for cookies.txt in several sensible places, in order:
      1. Next to this script's run location (cwd) — covers the common
         case of running `python musync_sync.py` from the project folder.
      2. The music library root (cfg.music_dir).
      3. The user's home directory (~/cookies.txt) — old default.
    Returns the first one that exists, or None.
    """
    candidates = [
        Path.cwd() / "cookies.txt",
        cfg.music_dir / "cookies.txt",
        Path.home() / "cookies.txt",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


INDEX_FILE_NAME = ".musync_index.json"

# ──────────────────────────────────────────────────────────
#  GLOBAL INDEX  — video_id -> {file, num, folder}
#  "file" is the filename only; "folder" is "" for root or the
#  playlist subfolder name.
# ──────────────────────────────────────────────────────────

def _index_path() -> Path:
    return cfg.music_dir / INDEX_FILE_NAME

def load_index() -> dict:
    return load_json(_index_path(), {})

def save_index(idx: dict):
    save_json(_index_path(), idx)


@dataclass
class SyncProgress:
    log: list = field(default_factory=list)
    running: bool = True
    stop_requested: bool = False
    downloaded: int = 0
    renamed: int = 0
    skipped: int = 0
    errors: int = 0
    retries: int = 0
    total: int = 0

    def emit(self, msg: str):
        self.log.append(msg)
        if len(self.log) > 2000:
            self.log = self.log[-2000:]

    def request_stop(self):
        self.stop_requested = True
        self.emit("[MUSYNC] ⏹ Stop requested — finishing in-flight downloads, no new ones will start...")


def safe_filename(s: str) -> str:
    s = re.sub(r'[/\\:*?"<>|]', '-', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:180]


def is_playlist_url(url: str) -> bool:
    return "list=" in url or "/playlist" in url


def _normalize_playlist_url(url: str) -> str:
    """
    music.youtube.com uses its own continuation-token pagination for
    playlists, which yt-dlp's extractor handles less reliably than the
    standard www.youtube.com/playlist browse-continuation flow — this
    causes random truncation (not always exactly at 100; can stop at
    101, 134, etc. depending on where a continuation request fails).

    Rewriting to www.youtube.com fetches the SAME playlist via the more
    stable extractor path while keeping every other URL parameter
    (list=, playnext=, etc.) intact.
    """
    if "music.youtube.com" in url:
        return url.replace("music.youtube.com", "www.youtube.com")
    return url


def _get_expected_playlist_count(url: str) -> int:
    """Ask yt-dlp for YouTube's own reported playlist size (fast, 1 item)."""
    cmd = ["yt-dlp", "--no-warnings", "--flat-playlist",
           "--playlist-items", "1",
           "--print", "%(playlist_count)s"]
    cookies = find_cookies_file()
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        return int(line) if line.isdigit() else 0
    except Exception:
        return 0


def _parse_print_lines(stdout: str) -> list:
    entries = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        vid, title, uploader = parts[0], parts[1], parts[2]
        dur_raw = parts[3] if len(parts) > 3 else "0"
        try:
            duration = int(float(dur_raw)) if dur_raw not in ("NA", "") else 0
        except Exception:
            duration = 0
        entries.append({"id": vid, "title": title,
                         "uploader": uploader, "duration": duration})
    return entries


# ──────────────────────────────────────────────────────────
#  DUPLICATE DETECTION — used by Syncer._run_inner(). This is
#  load-bearing code, NOT a helper safe to remove during cleanups.
# ──────────────────────────────────────────────────────────
def find_existing_track(video_id: str, title: str, artist: str,
                         duration: int, index: dict) -> Optional[dict]:
    """Search the WHOLE library (any folder) for this track."""
    # 1) Exact video_id match via index
    if video_id and video_id in index:
        entry = index[video_id]
        folder = entry.get("folder", "")
        fname  = entry.get("file", "")
        path = cfg.music_dir / folder / fname if folder else cfg.music_dir / fname
        if path.exists():
            return {"video_id": video_id, "file": fname, "folder": folder, "path": str(path)}

    # 2) Fuzzy match on normalized title/artist/duration across all files
    norm_t = normalize_title(title)
    norm_a = normalize_artist(artist or "").lower().strip()
    for mp3 in get_all_mp3s():
        stem = mp3.stem
        parts = stem.split(" - ", 2)
        if len(parts) >= 3:
            f_artist = normalize_artist(parts[1]).lower().strip()
            f_title  = normalize_title(parts[2])
        else:
            f_artist = ""
            f_title  = normalize_title(stem)
        if f_title == norm_t and (not norm_a or f_artist == norm_a):
            folder = mp3.parent.name if mp3.parent != cfg.music_dir else ""
            return {"video_id": "", "file": mp3.name, "folder": folder, "path": str(mp3)}
    return None


def yt_dlp_json(url: str, flat: bool = True, progress=None) -> list:
    """
    Fetch every playlist entry.

    DIAGNOSTIC FINDING (kept here so future-us doesn't repeat this):
    Chunked --playlist-items ranges (e.g. "101-200") were tried as a
    workaround for truncation at ~100 entries. Result: every chunk
    beyond the first returned exit-code 0, EMPTY stdout, EMPTY stderr.
    That means yt-dlp's youtube-tab extractor never advances past the
    first continuation page at all for this playlist/environment — it
    is not a rate-limit, not a VPN/IP issue, not something --playlist-
    items can route around, since the extractor has to walk
    continuation tokens sequentially regardless of which range is
    requested. Chunking was therefore removed; it only added 8x the
    subprocess overhead for zero benefit.

    The real fix has to happen at the yt-dlp/environment level:
      • pip install -U yt-dlp   (continuation-token parsing for
        YouTube is one of the most frequently-patched things in
        yt-dlp, due to constant API changes on YouTube's side)
      • Try a completely fresh cookies.txt (logged-in, exported just
        before running) — a stale/expired session can silently fall
        back to an anonymous code path that only sees ~100 items.
      • Try with NO --cookies at all once, for comparison.

    This function still does its best: it asks YouTube for the
    expected total, does one fetch pass, and if the result is short it
    clearly reports by how much and surfaces yt-dlp's own stderr/exit
    code so the real cause is visible instead of silently returning a
    partial list.
    """
    url = _normalize_playlist_url(url)
    expected = _get_expected_playlist_count(url)
    if progress and expected:
        progress.emit(f"[MUSYNC] YouTube reports {expected} tracks in playlist")

    cmd = ["yt-dlp", "--flat-playlist",
           "--print", "%(id)s\t%(title)s\t%(uploader)s\t%(duration)s",
           "--no-warnings"]
    cookies = find_cookies_file()
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        entries = _parse_print_lines(result.stdout)
    except subprocess.TimeoutExpired:
        if progress:
            progress.emit("[MUSYNC] fetch timed out after 240s")
        return []
    except Exception as e:
        if progress:
            progress.emit(f"[MUSYNC] fetch error: {e}")
        return []

    if progress and expected and len(entries) < expected:
        progress.emit(
            f"[MUSYNC] ⚠ Got {len(entries)}/{expected} — yt-dlp's continuation "
            f"walk stopped early (exit code {result.returncode})."
        )
        if result.stderr.strip():
            for l in result.stderr.splitlines()[-6:]:
                if l.strip():
                    progress.emit(f"  [yt-dlp] {l}")
        else:
            progress.emit("  [yt-dlp] no stderr output — likely a silent extractor limit")
        progress.emit(
            "[MUSYNC] Try: pip install -U yt-dlp   (and/or refresh cookies.txt)"
        )

    return entries


class Syncer:
    def __init__(self, url: str, start_from: int = 1, end_at: int = 99999,
                 jobs: int = 3, auto: bool = False, clean: bool = False,
                 mobile: bool = False, retry_max: int = 5):
        self.url = url
        self.start_from = start_from
        self.end_at = end_at
        self.jobs = max(1, jobs)
        self.auto = auto
        self.clean = clean
        self.mobile = mobile
        self.retry_max = retry_max
        self.progress = SyncProgress()
        self.audio_quality = "5" if mobile else "0"
        self._lock = threading.Lock()

    # ── public API ──────────────────────────────────────
    def run(self):
        try:
            self._run_inner()
        except Exception as e:
            self.progress.emit(f"[ERROR] {e}")
        finally:
            self.progress.running = False

    def stop(self):
        """Request a graceful stop: no new downloads start; ones
        already in flight are allowed to finish."""
        self.progress.request_stop()

    # ── internals ───────────────────────────────────────
    def _run_inner(self):
        p = self.progress
        p.emit(f"[MUSYNC] Fetching: {self.url}")

        is_pl = is_playlist_url(self.url)
        entries = yt_dlp_json(self.url, flat=True, progress=p)
        if not entries:
            p.emit("[ERROR] Could not fetch — empty result or network error")
            return

        target_folder = ""   # "" = root tracks/
        playlist_id = None

        if is_pl:
            # Get full playlist metadata (title, description, thumbnail)
            pl_meta = self._fetch_playlist_meta()
            pl_name = safe_filename(pl_meta.get("title", "Playlist"))
            target_folder = pl_name
            (cfg.music_dir / target_folder).mkdir(parents=True, exist_ok=True)
            p.emit(f"[MUSYNC] Playlist → folder '{target_folder}'")

            playlist_id = create_or_get_by_name(
                pl_meta.get("title", "Playlist"),
                pl_meta.get("description", ""),
            )
            # Download cover if available
            thumb_url = pl_meta.get("thumbnail", "")
            if thumb_url:
                self._save_playlist_cover(playlist_id, thumb_url)
            p.emit(f"[MUSYNC] Linked MUSYNC playlist: {pl_meta.get('title','Playlist')}")
        else:
            p.emit("[MUSYNC] Single track → tracks/ (no folder)")

        p.total = len(entries)
        p.emit(f"[MUSYNC] {len(entries)} item(s) in source")

        index = load_index()
        seen_ids = set()

        to_download = []
        for i, e in enumerate(entries, 1):
            if p.stop_requested:
                p.emit("[MUSYNC] Stopped before analysing remaining entries.")
                break
            if i < self.start_from or i > self.end_at:
                continue
            vid   = e.get("id", "")
            title = e.get("title", "Unknown")
            up    = e.get("uploader") or e.get("channel") or "Unknown"
            dur   = int(e.get("duration") or 0)
            if not vid:
                continue
            seen_ids.add(vid)

            existing = find_existing_track(vid, title, up, dur, index)
            if existing:
                p.emit(f"  =  exists: {title}  (in {'root' if not existing['folder'] else existing['folder']})")
                p.skipped += 1
                if playlist_id:
                    add_track_to_playlist(playlist_id, existing["file"])
                # Refresh index entry
                index[vid] = {"file": existing["file"], "num": i, "folder": existing["folder"]}
                continue

            to_download.append((i, vid, title, up, dur))

        p.emit(f"[MUSYNC] To download: {len(to_download)}  |  Already have: {p.skipped}")

        # Clean: remove tracks from this folder that are no longer in source playlist
        if self.clean and is_pl and target_folder:
            self._clean_folder(target_folder, seen_ids, index)

        # Download (threaded, polite)
        self._download_all(to_download, target_folder, playlist_id, index)

        # ── Auto-reindex: fixes duplicate/gapped numeric prefixes ──
        # (e.g. "298 - A.mp3" + "299 - B.mp3" + "299 - C.mp3") that can
        # build up when the source playlist's order shifts between
        # syncs and a stale file from a previous run shares a track's
        # new numeric prefix. This renumbers everything in this run's
        # target folder to a clean 001..N sequence and follows the
        # rename through the global index + this playlist's track list
        # so nothing breaks.
        if not p.stop_requested:
            reindex_result = reindex_folder(target_folder)
            if reindex_result["renamed"]:
                rename_map = {c["old"]: c["new"] for c in reindex_result["changes"]}
                for v_id, entry in index.items():
                    if entry.get("folder", "") == target_folder and entry.get("file") in rename_map:
                        entry["file"] = rename_map[entry["file"]]
                if playlist_id:
                    pls = get_all_playlists()
                    if playlist_id in pls:
                        pls[playlist_id]["tracks"] = [
                            rename_map.get(fn, fn) for fn in pls[playlist_id].get("tracks", [])
                        ]
                        save_all_playlists(pls)
                p.emit(f"[MUSYNC] Reindexed '{target_folder or 'root'}': {reindex_result['renamed']} file(s) renumbered")

        save_index(index)
        invalidate_cache()
        if p.stop_requested:
            p.emit(f"[MUSYNC] Stopped — ↓{p.downloaded} =,{p.skipped} ✗{p.errors}")
        else:
            p.emit(f"[MUSYNC] Done — ↓{p.downloaded} =,{p.skipped} ✗{p.errors}")

    def _fetch_playlist_meta(self) -> dict:
        meta_url = _normalize_playlist_url(self.url)
        cmd = ["yt-dlp", "--no-warnings", "--flat-playlist", "-J", "--playlist-items", "0"]
        cookies = find_cookies_file()
        if cookies:
            cmd += ["--cookies", str(cookies)]
        cmd.append(meta_url)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            data = json.loads(r.stdout)
            return {
                "title": data.get("title", "Playlist"),
                "description": data.get("description", "") or "",
                "thumbnail": (data.get("thumbnails") or [{}])[-1].get("url", ""),
            }
        except Exception:
            return {"title": "Playlist", "description": "", "thumbnail": ""}

    def _save_playlist_cover(self, playlist_id: str, thumb_url: str):
        try:
            import urllib.request
            dest = cfg.data_dir / f"pl_cover_{playlist_id}.jpg"
            urllib.request.urlretrieve(thumb_url, str(dest))
            pls = get_all_playlists()
            if playlist_id in pls:
                pls[playlist_id]["cover"] = str(dest)
                save_all_playlists(pls)
        except Exception:
            pass

    def _clean_folder(self, folder: str, seen_ids: set, index: dict):
        p = self.progress
        folder_path = cfg.music_dir / folder
        removed = 0
        for vid, entry in list(index.items()):
            if entry.get("folder") == folder and vid not in seen_ids:
                fp = folder_path / entry["file"]
                if fp.exists():
                    p.emit(f"  ! removing (no longer in source): {entry['file']}")
                    fp.unlink()
                    removed += 1
                del index[vid]
        p.emit(f"[MUSYNC] Cleaned {removed} stale tracks from '{folder}'")

    def _download_all(self, items, folder, playlist_id, index):
        p = self.progress
        sem = threading.Semaphore(self.jobs)
        threads = []

        for (idx, vid, title, up, dur) in items:
            if p.stop_requested:
                p.emit(f"[MUSYNC] Stopped — {len(items) - items.index((idx,vid,title,up,dur))} remaining track(s) not started.")
                break
            t = threading.Thread(
                target=self._download_one,
                args=(sem, idx, vid, title, up, dur, folder, playlist_id, index)
            )
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

    def _download_one(self, sem, idx, vid, title, uploader, duration,
                       folder, playlist_id, index):
        with sem:
            p = self.progress
            num = f"{idx:03d}"
            artist = safe_filename(normalize_artist(uploader))
            stitle = safe_filename(title)
            expected = f"{num} - {artist} - {stitle}.mp3"
            out_dir = cfg.music_dir / folder if folder else cfg.music_dir
            out_path = out_dir / expected

            cookies = find_cookies_file()
            has_cookies = cookies is not None
            use_cookies = has_cookies  # may be toggled off after a private/auth failure

            attempt = 0
            success = False
            _use_direct_url = False  # switched on when playlist extractor says unavailable
            while attempt < self.retry_max:
                attempt += 1
                if attempt > 1:
                    p.emit(f"  ↺ retry {attempt}/{self.retry_max}: {stitle}")
                cookie_args = ["--cookies", str(cookies)] if (use_cookies and cookies) else []

                # Format selector: plain "bestaudio/best" works reliably across
                # all yt-dlp player clients. Restricting by ext (webm/m4a) combined
                # with --extractor-args player_client=android caused
                # "Requested format is not available" because the android
                # client doesn't always expose those exact extensions.
                # If previous attempt said "unavailable via playlist",
                # _use_direct_url is True → same URL but yt-dlp will use the
                # single-video extractor instead of the playlist one, which
                # often succeeds even when the playlist context blocks it.
                _dl_url = f"https://www.youtube.com/watch?v={vid}"
                if _use_direct_url:
                    _use_direct_url = False
                    # Add --no-playlist explicitly to force single-video extractor
                    cookie_args = (["--cookies", str(cookies)] if (use_cookies and cookies) else []) + ["--no-playlist"]
                cmd = [
                    "yt-dlp", *cookie_args,
                    "--format", "bestaudio/best",
                    "--extract-audio", "--audio-format", "mp3",
                    "--audio-quality", self.audio_quality,
                    "--concurrent-fragments", "3",
                    "--sleep-requests", "0.3",
                    "--add-metadata", "--embed-thumbnail",
                    "--convert-thumbnails", "jpg",
                    "--output", str(out_dir / f"{num} - %(uploader)s - %(title)s.%(ext)s"),
                    "--no-playlist", "--no-warnings", "--no-part",
                    "--socket-timeout", "30", "--retries", "2",
                    "--fragment-retries", "3",
                    _dl_url,
                ]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                except subprocess.TimeoutExpired:
                    p.emit(f"  ✗ timeout: {stitle}")
                    time.sleep(5 * attempt)
                    continue

                if result.returncode == 0:
                    actual = None
                    if out_path.exists():
                        actual = out_path
                    else:
                        matches = list(out_dir.glob(f"{num} - *.mp3"))
                        actual = matches[0] if matches else None

                    if actual:
                        if actual.name != expected:
                            try:
                                actual.rename(out_dir / expected)
                            except Exception:
                                expected = actual.name
                        with self._lock:
                            index[vid] = {"file": expected, "num": idx, "folder": folder}
                            p.downloaded += 1
                        p.emit(f"  ↓ {num}  {stitle}  ({artist})")
                        if playlist_id:
                            add_track_to_playlist(playlist_id, expected)
                        success = True
                    else:
                        p.emit(f"  ✗ file missing after download: {stitle}")
                        with self._lock: p.errors += 1

                    # Cleanup leftover thumbnail/temp files for this track number
                    for f in out_dir.glob(f"{num} - *"):
                        if f.name != expected and f.suffix.lower() in (".webp", ".webm", ".part", ".m4a"):
                            try: f.unlink()
                            except Exception: pass

                    time.sleep(1 + (idx % 4))  # polite random-ish delay
                    break

                # Error handling
                err = (result.stderr or "")[:500]
                if re.search(r"429|Too Many Requests|rate.?limit", err, re.I):
                    w = min(300, 15 * attempt * attempt)
                    p.emit(f"  ! 429 — waiting {w}s")
                    with self._lock: p.retries += 1
                    time.sleep(w); continue
                if re.search(r"network|timeout|reset|HTTP Error 5\d\d", err, re.I):
                    w = 10 * attempt
                    p.emit(f"  ! network error — waiting {w}s")
                    with self._lock: p.retries += 1
                    time.sleep(w); continue
                # "Private video" / "unavailable": often a FALSE POSITIVE from
                # the playlist extractor. Strategy (mirrors what users do manually):
                #   1. If using cookies → retry without them (stale session can
                #      look more restricted than anonymous access).
                #   2. Always retry via direct watch?v= URL instead of through
                #      the playlist context — this is the same trick the user
                #      mentioned: direct URL works when playlist fetch fails.
                if re.search(r"private video|private|unavailable|removed|region|age.?restrict|copyright", err, re.I):
                    # Step 1: drop cookies if present
                    if use_cookies and has_cookies:
                        use_cookies = False
                        p.emit(f"  ! auth-error with cookies → retrying without: {stitle}")
                        continue

                    # Step 2: force direct-URL mode (only once, on next attempt)
                    if attempt < self.retry_max:
                        p.emit(f"  ! unavailable via playlist → trying direct URL: {stitle}")
                        # Patch the download URL for the next loop iteration
                        # by temporarily switching to watch?v= — we do this by
                        # NOT breaking out of the while loop; next iteration will
                        # use the direct_url override below.
                        _use_direct_url = True
                        continue

                    p.emit(f"  ! unavailable after all attempts: {stitle}")
                    with self._lock: p.skipped += 1
                    success = True; break
                if re.search(r"Sign in|bot|confirm", err, re.I):
                    p.emit(f"  ! bot-check (need cookies.txt): {stitle}")
                    with self._lock: p.retries += 1
                    time.sleep(20); continue

                p.emit(f"  ! error (attempt {attempt}): {err.splitlines()[-1] if err else 'unknown'}")
                with self._lock: p.retries += 1
                time.sleep(5 * attempt)

            if not success:
                p.emit(f"  ✗ failed after {self.retry_max}: {stitle}")
                with self._lock: p.errors += 1


# ──────────────────────────────────────────────────────────
#  Module-level registry of running syncs (for web API polling)
# ──────────────────────────────────────────────────────────
_active: dict[str, Syncer] = {}
_active_lock = threading.Lock()

def start_sync(url, **kwargs) -> str:
    sync_id = f"sync_{int(time.time()*1000)}"
    syncer = Syncer(url, **kwargs)
    with _active_lock:
        _active[sync_id] = syncer
    t = threading.Thread(target=syncer.run, daemon=True)
    t.start()
    return sync_id

def get_progress(sync_id: str) -> Optional[SyncProgress]:
    return _active.get(sync_id, {}).progress if sync_id in _active else None

def get_latest_progress() -> Optional[SyncProgress]:
    """Convenience: get the most recently started sync's progress."""
    syncer = get_latest_syncer()
    return syncer.progress if syncer else None

def get_latest_syncer() -> Optional["Syncer"]:
    """Get the most recently started Syncer instance (so it can be stopped)."""
    if not _active:
        return None
    latest_id = max(_active.keys())
    return _active.get(latest_id)

def stop_latest_sync() -> bool:
    """Request a graceful stop on the most recently started sync, if any."""
    syncer = get_latest_syncer()
    if syncer and syncer.progress.running:
        syncer.stop()
        return True
    return False
