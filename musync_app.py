#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MUSYNC v10.0 — Flask backend (modular)

Thin HTTP layer over musync_core/. All business logic lives in the
musync_core package; this file only translates HTTP <-> Python calls.

pip install flask mutagen requests pillow --break-system-packages
python3 musync_app.py
"""

import sys
import io
import json
import threading
import time
import zipfile
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, send_file, request, Response, make_response

from musync_core.config import cfg
from musync_core.storage import load_json, save_json, safe_path
from musync_core import tracks as T
from musync_core.tracks import mark_streaming, unmark_streaming, resolve_track_path
from musync_core import stats as S
from musync_core import lyrics as L
from musync_core import playlists as P
from musync_core import duplicates as D
from musync_core import cover_color as CC
from musync_core import sync as SY

try:
    from mutagen.id3 import ID3, APIC
    MUTAGEN = True
except ImportError:
    MUTAGEN = False

try:
    import requests as rq
    REQUESTS = True
except ImportError:
    REQUESTS = False

try:
    from PIL import Image
    PIL = True
except ImportError:
    PIL = False

app = Flask(__name__)

# ── SECURITY HEADERS ──────────────────────────────────
@app.after_request
def add_headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    return r

def enc(s): return s

# ════════════════════════════════════════════════════════
#  TRACKS
# ════════════════════════════════════════════════════════
import hashlib

VALID_SORTS = {"filename", "title", "artist", "album", "duration", "number"}

@app.route("/api/tracks")
def api_tracks():
    tl = T.get_tracks()

    # Optional server-side sort (?sort=title&asc=1)
    sort_by = request.args.get("sort", "filename")
    sort_asc = request.args.get("asc", "1") != "0"
    if sort_by in VALID_SORTS:
        rev = not sort_asc
        tl = sorted(tl, key=lambda t: (t.get(sort_by) or ""), reverse=rev)

    meta_mtime = str(cfg.meta_file.stat().st_mtime) if cfg.meta_file.exists() else "0"
    etag = hashlib.md5(
        (json.dumps([t["filename"] for t in tl]) + meta_mtime + sort_by + str(sort_asc)).encode()
    ).hexdigest()[:16]
    if request.headers.get("If-None-Match") == etag:
        return ("", 304)
    resp = make_response(jsonify(tl))
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.route("/api/tracks/refresh")
def api_tracks_refresh():
    return jsonify(T.get_tracks(force=True))

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").lower().strip()
    if not q:
        return jsonify([])
    tl = T.get_tracks()
    results = [
        t for t in tl
        if q in t.get("title","").lower()
        or q in t.get("artist","").lower()
        or q in t.get("album","").lower()
    ]
    return jsonify(results[:100])

# ════════════════════════════════════════════════════════
#  COVERS
# ════════════════════════════════════════════════════════
@app.route("/api/cover/<path:fn>")
def api_cover(fn):
    """Full-resolution cover — used for player art, fullscreen blur
    background, etc. where native resolution matters."""
    data = T.get_cover_data(fn)
    if data:
        return send_file(io.BytesIO(data), mimetype="image/jpeg")
    return ("", 404)

@app.route("/api/thumb/<path:fn>")
def api_thumb(fn):
    """
    Normalized SQUARE thumbnail — every artist/album/track cover comes
    back at the exact same size regardless of source resolution: small
    covers are upscaled (smooth LANCZOS), large ones downscaled, both
    through the same code path. Use this for grid/list views (Albums,
    Artists, track rows) instead of /api/cover for visual consistency.
    Query param ?size=N overrides the default 300px.
    """
    size = request.args.get("size", "300")
    try:
        size = max(40, min(800, int(size)))
    except ValueError:
        size = 300
    data = T.get_cover_data(fn)
    if not data:
        return ("", 404)
    thumb = CC.get_thumbnail(Path(fn).stem, data, size)
    return send_file(io.BytesIO(thumb), mimetype="image/jpeg")

@app.route("/api/cover/<path:fn>", methods=["POST"])
def api_cover_save(fn):
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    dest = cfg.covers_dir / (Path(fn).stem + ".jpg")
    request.files["file"].save(str(dest))
    stem = Path(fn).stem
    CC.invalidate_color(stem)
    CC.invalidate_thumbnails(stem)
    T.invalidate_cache()
    return jsonify({"ok": True})

@app.route("/api/cover-color/<path:fn>")
def api_cover_color(fn):
    stem = Path(fn).stem
    cached = CC.get_cached_color(stem)
    if cached:
        return jsonify({"color": cached})
    data = T.get_cover_data(fn)
    color = CC.get_dominant_color(data) if data else ""
    if color:
        CC.set_cached_color(stem, color)
    return jsonify({"color": color})

# ════════════════════════════════════════════════════════
#  STREAM / VIDEO
# ════════════════════════════════════════════════════════
@app.route("/stream/<path:fn>")
def stream(fn):
    """
    O(1) track resolution via in-memory filename→Path index.
    No disk scan on every Range request (battery-critical for mobile).
    Marks the file as in-use so reindex won't move it while streaming.
    """
    safe_fn = Path(fn).name  # strip any path components — path traversal guard
    path = resolve_track_path(safe_fn)
    if not path or not path.exists():
        return ("", 404)
    mark_streaming(safe_fn)
    try:
        return send_file(str(path), mimetype="audio/mpeg", conditional=True)
    finally:
        # Flask streaming is synchronous per-request — this runs after
        # the full response is sent (or on error), so it is safe.
        unmark_streaming(safe_fn)

@app.route("/video/<path:fn>")
def video(fn):
    safe_fn = Path(fn).name
    stem    = Path(safe_fn).stem
    path    = resolve_track_path(safe_fn)
    if path:
        for ext, mime in ((".mp4","video/mp4"), (".webm","video/webm"), (".mkv","video/mp4")):
            vp = path.parent / (stem + ext)
            if vp.exists():
                return send_file(str(vp), mimetype=mime, conditional=True)
    return ("", 404)

@app.route("/api/video/upload/<path:fn>", methods=["POST"])
def api_video_upload(fn):
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    stem = Path(fn).stem
    ext = Path(request.files["file"].filename).suffix.lower() or ".mp4"
    if ext not in (".mp4", ".webm", ".mkv"): ext = ".mp4"
    # Save next to the matching mp3
    out_dir = cfg.music_dir
    for mp3 in T.get_all_mp3s():
        if mp3.stem == stem:
            out_dir = mp3.parent
            break
    request.files["file"].save(str(out_dir / (stem + ext)))
    T.invalidate_cache()
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════════
#  METADATA EDIT
# ════════════════════════════════════════════════════════
@app.route("/api/meta/<path:fn>", methods=["POST"])
def api_meta_save(fn):
    data = request.get_json(silent=True) or {}
    safe_fn = Path(fn).name  # strip any path components
    T.save_meta(safe_fn, data)
    return jsonify({"ok": True})

@app.route("/api/normalize", methods=["POST"])
def api_normalize():
    data = request.get_json(silent=True) or {}
    def norm(s):
        import re as _re
        s = str(s or "").strip()
        s = _re.sub(r" - Topic$", "", s, flags=_re.I)
        s = _re.sub(r"\(Official.*?\)", "", s, flags=_re.I)
        s = _re.sub(r"\s+", " ", s).strip()
        return s
    return jsonify({
        "title":  norm(data.get("title","")),
        "artist": norm(data.get("artist","")),
        "album":  norm(data.get("album","")),
    })

# ════════════════════════════════════════════════════════
#  LYRICS
# ════════════════════════════════════════════════════════
@app.route("/api/lyrics")
def api_lyrics():
    fn     = request.args.get("fn", "")
    title  = request.args.get("title", "")
    artist = request.args.get("artist", "")
    album  = request.args.get("album", "")
    dur    = request.args.get("duration", "")
    if fn and (not album or not dur or not title or not artist):
        for t in T.get_tracks():
            if t.get("filename") == fn:
                title  = title  or t.get("title", "")
                artist = artist or t.get("artist", "")
                album  = album  or t.get("album", "")
                dur    = dur    or t.get("duration", 0)
                break
    try:
        dur = float(dur) if dur else 0
    except Exception:
        dur = 0
    return jsonify(L.get_lyrics(fn, title, artist, album, dur))

@app.route("/api/lyrics/save", methods=["POST"])
def api_lyrics_save():
    data = request.get_json(silent=True) or {}
    fn = Path(data.get("fn", "")).name   # strip path (traversal guard)
    if not fn: return jsonify({"error": "no fn"}), 400
    L.save_lyrics(fn, data.get("text",""), data.get("synced", False))
    return jsonify({"ok": True})

@app.route("/api/lyrics/delete", methods=["POST"])
def api_lyrics_delete():
    fn = Path((request.get_json(silent=True) or {}).get("fn", "")).name
    L.delete_lyrics(fn)
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════════
#  HISTORY / STATS
# ════════════════════════════════════════════════════════
@app.route("/api/history/add", methods=["POST"])
def api_history_add():
    data = request.get_json(silent=True) or {}
    fn = data.get("fn", "")
    if not fn: return jsonify({"error": "no fn"}), 400
    S.add_history(fn, data.get("pct", 0), data.get("device", ""))
    return jsonify({"ok": True})

@app.route("/api/history")
def api_history():
    limit = int(request.args.get("limit", 100))
    h = load_json(cfg.hist_file, [])
    return jsonify(h[-limit:])

@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    S.clear_history()
    return jsonify({"ok": True})

@app.route("/api/stats")
def api_stats():
    device = request.args.get("device", "")
    return jsonify(S.get_stats(device))

@app.route("/api/stats/year")
def api_stats_year():
    year = int(request.args.get("year", datetime.now().year))
    return jsonify(S.get_yearly_stats(year))

# ════════════════════════════════════════════════════════
#  PLAYLISTS
# ════════════════════════════════════════════════════════
@app.route("/api/playlists")
def api_playlists():
    pls = P.get_all_with_counts()
    sort_by  = request.args.get("sort", "name")
    sort_asc = request.args.get("asc", "1") != "0"
    items = list(pls.items())
    if sort_by == "file_count":
        items.sort(key=lambda x: x[1].get("file_count", 0), reverse=not sort_asc)
    else:
        items.sort(key=lambda x: (x[1].get("name") or "").lower(), reverse=not sort_asc)
    return jsonify(dict(items))

@app.route("/api/playlists/save", methods=["POST"])
def api_playlists_save():
    P.save_all(request.get_json(silent=True) or {})
    return jsonify({"ok": True})

@app.route("/api/playlists/<pl_id>/edit", methods=["POST"])
def api_playlist_edit(pl_id):
    data = request.get_json(silent=True) or {}
    ok = P.edit_playlist(pl_id, data)
    return jsonify({"ok": ok}) if ok else (jsonify({"error":"not found"}), 404)

@app.route("/api/playlists/<pl_id>/cover", methods=["POST"])
def api_playlist_cover_save(pl_id):
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    path = P.save_cover(pl_id, request.files["file"])
    return jsonify({"ok": True, "path": path})

@app.route("/api/playlists/<pl_id>/cover")
def api_playlist_cover_get(pl_id):
    path = P.get_cover_path(pl_id)
    if path:
        return send_file(path, mimetype="image/jpeg")
    return ("", 404)

@app.route("/api/liked/toggle", methods=["POST"])
def api_liked_toggle():
    fn = (request.get_json(silent=True) or {}).get("fn", "")
    if not fn: return jsonify({"error": "no fn"}), 400
    return jsonify(P.toggle_liked(fn))

@app.route("/api/liked/status")
def api_liked_status():
    fn = request.args.get("fn", "")
    return jsonify({"liked": P.is_liked(fn)})

# ════════════════════════════════════════════════════════
#  ALIASES
# ════════════════════════════════════════════════════════
@app.route("/api/aliases")
def api_aliases():
    return jsonify(load_json(cfg.aliases_file, {}))

@app.route("/api/aliases/save", methods=["POST"])
def api_aliases_save():
    save_json(cfg.aliases_file, request.get_json(silent=True) or {})
    T.invalidate_cache()
    return jsonify({"ok": True})

# ── Auto aliases: same shape ({raw: clean}), applied automatically to
# every matching artist name (no per-track confirmation needed) — but,
# same as manual aliases, ONLY for names explicitly listed here, so
# nothing is ever renamed without the user having added it.
@app.route("/api/aliases/auto")
def api_aliases_auto():
    return jsonify(load_json(cfg.auto_aliases_file, {}))

@app.route("/api/aliases/auto/save", methods=["POST"])
def api_aliases_auto_save():
    save_json(cfg.auto_aliases_file, request.get_json(silent=True) or {})
    T.invalidate_cache()
    return jsonify({"ok": True})

@app.route("/api/artists")
def api_artists():
    """Individual artist names (already split) + track counts.
    ?sort=name (default) or ?sort=count, ?asc=0 to reverse."""
    artists  = T.get_unique_artists()
    sort_by  = request.args.get("sort", "name")
    sort_asc = request.args.get("asc", "1") != "0"
    if sort_by == "count":
        artists.sort(key=lambda a: a["count"], reverse=not sort_asc)
    else:
        artists.sort(key=lambda a: a["name"].lower(), reverse=not sort_asc)
    return jsonify(artists)

# ════════════════════════════════════════════════════════
#  DUPLICATES
# ════════════════════════════════════════════════════════
@app.route("/api/albums")
def api_albums():
    """Return sorted album list derived from the track cache.
    ?sort=name (default) | title | count | artist, ?asc=0 to reverse."""
    tl = T.get_tracks()
    albums: dict = {}
    for t in tl:
        al = t.get("album", "").strip()
        if not al:
            continue
        if al not in albums:
            albums[al] = {"name": al, "artist": t.get("artist",""), "cover": t["filename"], "tracks": []}
        albums[al]["tracks"].append(t["filename"])

    sort_by  = request.args.get("sort", "name")
    sort_asc = request.args.get("asc", "1") != "0"
    items = list(albums.values())
    if sort_by == "count":
        items.sort(key=lambda a: len(a["tracks"]), reverse=not sort_asc)
    elif sort_by == "artist":
        items.sort(key=lambda a: (a["artist"] or "").lower(), reverse=not sort_asc)
    else:
        items.sort(key=lambda a: a["name"].lower(), reverse=not sort_asc)
    return jsonify(items)

@app.route("/api/duplicates")
def api_duplicates():
    return jsonify(D.find_duplicates())

@app.route("/api/reindex", methods=["POST"])
def api_reindex():
    """Renumber a folder's mp3s to a clean 001..N sequence, fixing
    duplicate/gapped prefixes left over from past syncs. Pass
    {"folder": "Playlist Name"} or {} / {"folder": ""} for the root,
    or {"all": true} to reindex every folder in the library."""
    data = request.get_json(silent=True) or {}
    if data.get("all"):
        result = T.reindex_all()
    else:
        folder = data.get("folder", "")
        result = T.reindex_folder(folder)
    T.invalidate_cache()
    return jsonify(result)

# ════════════════════════════════════════════════════════
#  SETTINGS / MUSIC_DIR
# ════════════════════════════════════════════════════════
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    s = cfg.load_settings()
    s["music_dir"] = str(cfg.music_dir)
    return jsonify(s)

@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.get_json(silent=True) or {}
    if "music_dir" in data:
        cfg.music_dir = data["music_dir"]
        T.invalidate_cache()
    cfg.save_settings(data)
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════════
#  THEME — user-supplied CSS, injected client-side as the
#  last stylesheet in <head> so it can override anything
#  (colors, layout, the lyrics view, etc). Settings → Theme.
# ════════════════════════════════════════════════════════
MAX_THEME_CSS_BYTES = 300_000  # plenty for a stylesheet, small enough to be safe

@app.route("/api/theme/css")
def api_theme_css_get():
    css = ""
    if cfg.theme_css_file.exists():
        try:
            css = cfg.theme_css_file.read_text("utf-8")
        except Exception:
            css = ""
    resp = make_response(css)
    resp.headers["Content-Type"] = "text/css; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/theme/css", methods=["POST"])
def api_theme_css_save():
    if "file" in request.files:
        raw = request.files["file"].read()
    else:
        raw = request.get_data() or b""
    if len(raw) > MAX_THEME_CSS_BYTES:
        return jsonify({"error": "CSS too large (max 300KB)"}), 400
    css = raw.decode("utf-8", errors="ignore")
    cfg.theme_css_file.write_text(css, "utf-8")
    return jsonify({"ok": True, "bytes": len(raw)})

@app.route("/api/theme/css", methods=["DELETE"])
def api_theme_css_clear():
    try:
        if cfg.theme_css_file.exists():
            cfg.theme_css_file.unlink()
    except Exception:
        pass
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════════
#  QUEUE PERSISTENCE
# ════════════════════════════════════════════════════════
@app.route("/api/queue", methods=["GET"])
def api_queue_get():
    return jsonify(load_json(cfg.queue_file, {"queue": [], "idx": -1}))

@app.route("/api/queue", methods=["POST"])
def api_queue_save():
    save_json(cfg.queue_file, request.get_json(silent=True) or {})
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════════
#  SYNC — now powered by musync_core.sync (pure Python,
#  no bash subprocess). Handles playlist-folder logic,
#  cross-folder duplicate detection, auto playlist creation.
# ════════════════════════════════════════════════════════
_current_sync_id = None

@app.route("/api/sync/start", methods=["POST"])
def api_sync_start():
    global _current_sync_id
    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    if not url:
        return jsonify({"status": "error", "msg": "no url"})

    existing = SY.get_latest_progress()
    if existing and existing.running:
        return jsonify({"status": "already_running"})

    _current_sync_id = SY.start_sync(
        url,
        start_from=int(data.get("from", 1)),
        end_at=int(data.get("to", 99999)),
        jobs=int(data.get("jobs", 3)),
        auto=bool(data.get("auto", False)),
        clean=bool(data.get("clean", False)),
        mobile=bool(data.get("mobile", False)),
    )
    return jsonify({"status": "started", "sync_id": _current_sync_id})

@app.route("/api/sync/log")
def api_sync_log():
    p = SY.get_latest_progress()
    if not p:
        return jsonify({"running": False, "log": []})
    return jsonify({
        "running": p.running,
        "stop_requested": p.stop_requested,
        "log": p.log,
        "downloaded": p.downloaded,
        "skipped": p.skipped,
        "errors": p.errors,
        "retries": p.retries,
        "total": p.total,
    })

@app.route("/api/sync/stop", methods=["POST"])
def api_sync_stop():
    ok = SY.stop_latest_sync()
    return jsonify({"ok": ok, "msg": "Stopping..." if ok else "No active sync to stop"})

# ════════════════════════════════════════════════════════
#  NGROK / TUNNEL STATUS
# ════════════════════════════════════════════════════════
@app.route("/api/ngrok/status")
def api_ngrok_status():
    if not REQUESTS:
        return jsonify({"active": False, "urls": []})
    try:
        r = rq.get("http://localhost:4040/api/tunnels", timeout=2)
        tunnels = r.json().get("tunnels", [])
        urls = [t["public_url"] for t in tunnels if "public_url" in t]
        return jsonify({"active": True, "urls": urls})
    except Exception:
        return jsonify({"active": False, "urls": []})

# ════════════════════════════════════════════════════════
#  BACKUP
# ════════════════════════════════════════════════════════
@app.route("/api/backup")
def api_backup():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in cfg.data_dir.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(cfg.data_dir))
    buf.seek(0)
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name=f"musync_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
    )

# ════════════════════════════════════════════════════════
#  ONLINE SEARCH (yt-dlp ytsearch)
# ════════════════════════════════════════════════════════
import subprocess, shutil as _shutil, re as _re

def _yt_search(query: str, limit: int = 20) -> list:
    try:
        search_url = f"ytsearch{limit}:{query}"
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--print",
             "%(id)s\t%(title)s\t%(uploader)s\t%(duration)s",
             "--no-warnings", search_url],
            capture_output=True, text=True, timeout=15
        )
        items = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 3: continue
            vid_id, title, uploader = parts[0], parts[1], parts[2]
            dur = parts[3] if len(parts) > 3 else "0"
            if not vid_id: continue
            try: duration = int(float(dur)) if dur not in ("NA","") else 0
            except Exception: duration = 0
            items.append({
                "id": vid_id, "title": title,
                "artist": _re.sub(r" - Topic$", "", uploader, flags=_re.I),
                "duration": duration,
                "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={vid_id}",
            })
        return items
    except Exception:
        return []

@app.route("/api/online/search")
def api_online_search():
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 20)), 50)
    if not q: return jsonify([])
    if not _shutil.which("yt-dlp"):
        return jsonify({"error": "yt-dlp not found"}), 503
    return jsonify(_yt_search(q, limit))

@app.route("/api/online/stream/<vid_id>")
def api_online_stream(vid_id):
    if not _shutil.which("yt-dlp"):
        return jsonify({"error": "yt-dlp not found"}), 503
    try:
        result = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "-g", "--no-warnings",
             f"https://www.youtube.com/watch?v={vid_id}"],
            capture_output=True, text=True, timeout=15
        )
        stream_url = result.stdout.strip().split("\n")[0]
        if not stream_url:
            return jsonify({"error": "no stream"}), 404
        return jsonify({"url": stream_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/online/download", methods=["POST"])
def api_online_download():
    """Download a single online track directly into tracks/ (uses sync logic for dedup)."""
    data = request.get_json(silent=True) or {}
    vid_id = data.get("id", "")
    title  = data.get("title", "unknown")
    artist = data.get("artist", "unknown")
    if not vid_id:
        return jsonify({"error": "no id"}), 400
    url = f"https://www.youtube.com/watch?v={vid_id}"
    sync_id = SY.start_sync(url, jobs=1)
    return jsonify({"ok": True, "msg": f"Downloading {title}...", "sync_id": sync_id})

# ════════════════════════════════════════════════════════
#  PWA / ROOT
# ════════════════════════════════════════════════════════
@app.route("/")
def index():
    ui = Path(__file__).parent / "musync_ui.html"
    _ensure_manifest()
    if ui.exists():
        return send_file(str(ui))
    return Response("<h1>musync_ui.html not found</h1>", status=404)

def _ensure_manifest():
    mf = Path(__file__).parent / "manifest.json"
    if not mf.exists():
        mf.write_text(json.dumps({
            "name": "MUSYNC", "short_name": "MUSYNC", "start_url": "/",
            "display": "standalone", "background_color": "#0a0a0a",
            "theme_color": "#e50914",
            "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}]
        }, indent=2))

@app.route("/manifest.json")
def manifest():
    _ensure_manifest()
    return send_file(str(Path(__file__).parent / "manifest.json"),
                     mimetype="application/manifest+json")

@app.route("/sw.js")
def sw():
    js = """
self.addEventListener('install', e => {
  e.waitUntil(caches.open('musync-v2').then(c => c.addAll(['/'])));
});
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/stream/') || e.request.url.includes('/video/')) return;
  if (e.request.url.includes('/api/tracks') || e.request.url.includes('/api/cover')) {
    e.respondWith(
      caches.open('musync-data').then(cache =>
        fetch(e.request).then(r => { cache.put(e.request, r.clone()); return r })
        .catch(() => caches.match(e.request))
      )
    );
    return;
  }
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
"""
    return Response(js, mimetype="application/javascript")

@app.route("/api/config")
def api_config():
    return jsonify({
        "music_dir": str(cfg.music_dir),
        "mutagen": MUTAGEN, "pillow": PIL, "requests": REQUESTS,
    })

# ════════════════════════════════════════════════════════
#  STARTUP — must be last (after all @app.route definitions)
# ════════════════════════════════════════════════════════
if __name__ == "__main__":
    w = 54
    print()
    print("  ╔" + "═" * w + "╗")
    print("  ║" + "  ♫  MUSYNC  v10.0  (modular)".center(w) + "║")
    print("  ╠" + "═" * w + "╣")
    print("  ║" + f"  music  : {cfg.music_dir}".ljust(w) + "║")
    print("  ║" + f"  mutagen: {'✓' if MUTAGEN  else '✗  pip install mutagen'}".ljust(w) + "║")
    print("  ║" + f"  pillow : {'✓' if PIL      else '✗  pip install pillow'}".ljust(w) + "║")
    print("  ║" + f"  request: {'✓' if REQUESTS else '✗  pip install requests'}".ljust(w) + "║")
    print("  ╠" + "═" * w + "╣")
    print("  ║" + "  → http://localhost:5000".ljust(w) + "║")
    print("  ╚" + "═" * w + "╝")
    print()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
