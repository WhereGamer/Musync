"""
Track scanning, parsing and library management.

Battery/IO optimisation notes for Termux/Android:
 - get_tracks() is the ONLY function that touches disk in a scan loop.
   Everything else (including the /stream/ route) uses the in-memory
   cache or a pre-built filename→path index so a single Range request
   does NOT re-scan the library (O(1) lookup, not O(N)).
 - Cache TTL raised to 5 minutes. Tag parsing (Mutagen) is expensive
   on Android; there is no reason to repeat it every 20 s.
 - _PATH_INDEX maps filename → full Path so the stream route can
   resolve a track in O(1) without a second glob.
 - File renames (reindex) always go through _rename_safe() which
   waits for a flag to be clear, so a file is never moved while
   it is being streamed.
"""
import re
import io
import time
import threading
from pathlib import Path
from typing import Optional

try:
    from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB
    from mutagen.mp3 import MP3
    MUTAGEN = True
except ImportError:
    MUTAGEN = False

from .storage import load_json, save_json, safe_path
from .config import cfg

# ── Primary track cache ────────────────────────────────────
_cache: Optional[list]  = None
_cache_time: float      = 0
_cache_lock             = threading.Lock()
_PATH_INDEX: dict[str, Path] = {}   # filename → absolute Path (O(1) lookup)
CACHE_TTL               = 300       # 5 minutes (was 20 s — huge battery win)

# ── Rename guard: prevents moving a file while it is streaming ─
_streaming: set[str]  = set()        # filenames currently being read
_stream_lock          = threading.Lock()

def mark_streaming(fn: str):
    with _stream_lock:
        _streaming.add(fn)

def unmark_streaming(fn: str):
    with _stream_lock:
        _streaming.discard(fn)

def _rename_safe(src: Path, dst: Path, retries: int = 8) -> bool:
    """Rename src→dst, waiting up to ~4 s if src is currently streaming."""
    fn = src.name
    for _ in range(retries):
        with _stream_lock:
            if fn not in _streaming:
                try:
                    src.rename(dst)
                    return True
                except Exception:
                    return False
        time.sleep(0.5)
    return False  # gave up — file still streaming after retries

# ── Artist / title helpers ────────────────────────────────
def split_artists(artist_str: str) -> list:
    """'A & B feat. C' → ['A', 'B', 'C']"""
    if not artist_str:
        return []
    s = artist_str.replace("，", ",").replace("；", ";")
    parts = re.split(
        r',|;|&|\bfeat\.?\s*|\bft\.?\s*|\bwith\b|\bvs\.?\b|\bx\b'
        r'|\bпри уч\.\s*|\bпри участии\s*',
        s, flags=re.IGNORECASE
    )
    return [p.strip().strip("()[]").strip() for p in parts if p.strip().strip("()[]").strip()]


def normalize_artist(name: str) -> str:
    name = re.sub(r" - Topic$",       "", name, flags=re.IGNORECASE)
    name = re.sub(r" \(Official\)$",  "", name, flags=re.IGNORECASE)
    return name.strip()


def normalize_title(title: str) -> str:
    t = title.lower()
    for pat in [
        r'\(official.*?\)', r'\[official.*?\]',
        r'\(feat\..*?\)', r'\(ft\..*?\)',
        r'\(lyrics?\)', r'\(audio\)', r'\(video\)',
        r'- topic$', r'\s+', r'[^a-zа-я0-9 ]',
    ]:
        t = re.sub(pat, ' ', t, flags=re.IGNORECASE)
    return t.strip()


# ── Single-track parser ───────────────────────────────────
def parse_track(f: Path, meta_store: dict = None, aliases: dict = None,
                 auto_aliases: dict = None, known_artists: dict = None) -> dict:
    """
    aliases       — manual artist renames (existing behaviour, always wins).
    auto_aliases  — "consented" automatic renames (e.g. 'rachie🎀💌' -> 'Rachie'),
                     applied the same way but lower priority than manual.
    known_artists — {normalized_artist_name: occurrence_count} across the
                     WHOLE library (built once per get_tracks() pass, cheap
                     filename-only parse — see _build_known_artists()).
                     Used to gate the "strip embedded artist name from title"
                     behaviour so it only fires for an artist we already have
                     more than one track from, never on the very first guess.
    """
    stem = f.stem
    playlist_folder = f.parent.name if f.parent != cfg.music_dir else ""

    parts = stem.split(" - ", 2)
    t: dict = {
        "filename":        f.name,
        "filepath":        str(f),
        "number":          0,
        "title":           stem,
        "artist":          "",
        "album":           "",
        "duration":        0,
        "has_video":       False,
        "artists":         [],
        "playlist_folder": playlist_folder,
    }

    if len(parts) >= 3 and parts[0].strip().isdigit():
        t["number"] = int(parts[0].strip())
        t["artist"] = normalize_artist(parts[1].strip())
        t["title"]  = parts[2].strip()

    if MUTAGEN:
        try:
            tags = ID3(str(f))
            if "TIT2" in tags: t["title"]  = str(tags["TIT2"])
            if "TPE1" in tags: t["artist"] = normalize_artist(str(tags["TPE1"]))
            if "TALB" in tags: t["album"]  = str(tags["TALB"])
        except Exception:
            pass
        try:
            t["duration"] = int(MP3(str(f)).info.length)
        except Exception:
            pass

    for ext in (".mp4", ".webm", ".mkv"):
        if (f.parent / (f.stem + ext)).exists():
            t["has_video"] = True
            break

    # Manual meta overrides (highest priority)
    if meta_store and f.name in meta_store:
        for k in ("title", "artist", "album"):
            if k in meta_store[f.name]:
                t[k] = meta_store[f.name][k]

    # Apply aliases per-individual artist (never on the raw combined string).
    # Auto-aliases apply first (lower priority), manual aliases always win —
    # both only ever touch a name that's explicitly listed by the user, so
    # nothing is ever silently renamed without that "consent".
    raw_parts = split_artists(t["artist"])
    if raw_parts:
        named = raw_parts
        if auto_aliases:
            named = [auto_aliases.get(a, a) for a in named]
        if aliases:
            named = [aliases.get(a, a) for a in named]
        t["artists"] = named
        if t["artists"]:
            t["artist"] = t["artists"][0]
    else:
        t["artists"] = raw_parts

    # Title cleanup: "Artist - Track" as the whole title (common with
    # YouTube uploads that embed the artist name in the video title) only
    # gets the artist prefix stripped once that artist is already a known,
    # repeated entry in the library — never on a lone/unconfirmed guess.
    if known_artists and t["artist"]:
        m = re.match(r"^\s*(.+?)\s*-\s*(.+)$", t["title"])
        if m:
            lead, rest = m.group(1).strip(), m.group(2).strip()
            lead_norm = normalize_artist(lead).lower()
            artist_norm = t["artist"].strip().lower()
            if rest and lead_norm == artist_norm and known_artists.get(artist_norm, 0) >= 2:
                t["title"] = rest

    # Album fallback: if there's no album tag, show the track's own title
    # instead of a blank field (e.g. single "Gori" -> album "Gori").
    if not t["album"] and t["title"]:
        t["album"] = t["title"]

    return t


def _build_known_artists(mp3s: list[Path], aliases: dict, auto_aliases: dict) -> dict:
    """Cheap filename-only pre-pass: counts how many files resolve to each
    (alias-applied) artist name, WITHOUT touching ID3 tags. Used only to
    gate the title artist-strip above — a full mutagen-based count isn't
    needed for a >=2 occurrence check."""
    counts: dict = {}
    for f in mp3s:
        parts = f.stem.split(" - ", 2)
        if len(parts) >= 3 and parts[0].strip().isdigit():
            artist = normalize_artist(parts[1].strip())
        else:
            continue
        for raw in split_artists(artist):
            name = auto_aliases.get(raw, raw) if auto_aliases else raw
            name = aliases.get(name, name) if aliases else name
            key = name.strip().lower()
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


# ── Library discovery ──────────────────────────────────────
def get_all_mp3s() -> list[Path]:
    """Root tracks/ + one level of playlist subfolders."""
    base   = cfg.music_dir
    result = list(base.glob("*.mp3"))
    for sub in base.iterdir():
        if sub.is_dir() and not sub.name.startswith('.'):
            result.extend(sub.glob("*.mp3"))
    return sorted(result)


def _rebuild_path_index(mp3s: list[Path]):
    """Rebuild the O(1) filename→path lookup used by the stream route.

    Collision handling: if two playlists contain '001 - Intro.mp3',
    the root-level file wins over the subfolder version.  Callers
    that need the playlist-specific file should pass the full relative
    path (e.g. 'PlaylistName/001 - Intro.mp3').
    """
    idx: dict[str, Path] = {}
    # Two passes: subfolders first, root second (root wins on collision)
    for mp3 in mp3s:
        if mp3.parent != cfg.music_dir:
            idx[mp3.name] = mp3
    for mp3 in mp3s:
        if mp3.parent == cfg.music_dir:
            idx[mp3.name] = mp3
        # Also index by "PlaylistFolder/filename" for exact lookup
        rel = mp3.parent.name + "/" + mp3.name if mp3.parent != cfg.music_dir else mp3.name
        idx[rel] = mp3
    return idx


def get_tracks(force: bool = False) -> list:
    global _cache, _cache_time, _PATH_INDEX
    now = time.time()
    with _cache_lock:
        if not force and _cache is not None and (now - _cache_time) < CACHE_TTL:
            return _cache
        if not cfg.music_dir.exists():
            _cache = []; _cache_time = now; return []
        meta         = load_json(cfg.meta_file,    {})
        aliases      = load_json(cfg.aliases_file, {})
        auto_aliases = load_json(cfg.auto_aliases_file, {})
        mp3s         = get_all_mp3s()
        known_artists = _build_known_artists(mp3s, aliases, auto_aliases)
        tracks  = []
        for f in mp3s:
            try:
                tracks.append(parse_track(f, meta, aliases, auto_aliases, known_artists))
            except Exception:
                pass
        _PATH_INDEX = _rebuild_path_index(mp3s)
        _cache      = tracks
        _cache_time = now
        return _cache


def resolve_track_path(fn: str) -> Optional[Path]:
    """O(1) filename → absolute Path lookup (no disk scan)."""
    with _cache_lock:
        if _PATH_INDEX:
            return _PATH_INDEX.get(fn)
    # Fallback if cache not yet built
    get_tracks()
    with _cache_lock:
        return _PATH_INDEX.get(fn)


def invalidate_cache():
    global _cache, _cache_time, _PATH_INDEX
    with _cache_lock:
        _cache      = None
        _cache_time = 0
        _PATH_INDEX = {}


# ── Artist picker for alias UI ────────────────────────────
def get_unique_artists() -> list:
    counts: dict = {}
    for t in get_tracks():
        names = t.get("artists") or ([t["artist"]] if t.get("artist") else [])
        for n in names:
            n = n.strip()
            if n:
                counts[n] = counts.get(n, 0) + 1
    return sorted(
        [{"name": n, "count": c} for n, c in counts.items()],
        key=lambda x: x["name"].lower()
    )


# ── Cover data ─────────────────────────────────────────────
def get_cover_data(fn: str) -> Optional[bytes]:
    custom = cfg.covers_dir / (Path(fn).stem + ".jpg")
    if custom.exists():
        return custom.read_bytes()
    # Use the O(1) path index instead of a full glob
    path = resolve_track_path(fn)
    if path and MUTAGEN:
        try:
            for tag in ID3(str(path)).values():
                if isinstance(tag, APIC):
                    return tag.data
        except Exception:
            pass
    return None


# ── Meta / tag write-back ─────────────────────────────────
def save_meta(fn: str, data: dict):
    m = load_json(cfg.meta_file, {})
    m.setdefault(fn, {}).update(data)
    save_json(cfg.meta_file, m)
    path = resolve_track_path(fn)
    if path and MUTAGEN:
        try:
            try:   tags = ID3(str(path))
            except Exception: tags = ID3()
            if "title"  in data: tags["TIT2"] = TIT2(encoding=3, text=data["title"])
            if "artist" in data: tags["TPE1"] = TPE1(encoding=3, text=data["artist"])
            if "album"  in data: tags["TALB"] = TALB(encoding=3, text=data["album"])
            tags.save(str(path))
        except Exception:
            pass
    invalidate_cache()


# ── Duplicate detection ───────────────────────────────────
def find_duplicate(title: str, artist: str, duration: int = 0) -> Optional[dict]:
    norm_t = normalize_title(title)
    norm_a = (artist or "").lower().strip()
    for t in get_tracks():
        if normalize_title(t["title"]) == norm_t:
            if not norm_a or (t["artist"] or "").lower().strip() == norm_a:
                if not duration or abs(t["duration"] - duration) < 5:
                    return t
    return None


# ── Reindex (two-pass, rename-safe) ──────────────────────
def reindex_folder(folder: str = "") -> dict:
    target_dir = cfg.music_dir / folder if folder else cfg.music_dir
    if not target_dir.exists():
        return {"renamed": 0, "changes": []}

    def sort_key(p: Path):
        parts = p.stem.split(" - ", 2)
        num   = int(parts[0]) if parts and parts[0].strip().isdigit() else 999999
        return (num, p.stem.lower())

    mp3s = sorted(target_dir.glob("*.mp3"), key=sort_key)
    changes = []
    temp_map = []

    for i, mp3 in enumerate(mp3s, 1):
        new_num  = f"{i:03d}"
        parts    = mp3.stem.split(" - ", 2)
        rest     = " - ".join(parts[1:]) if len(parts) > 1 else mp3.stem
        new_stem = f"{new_num} - {rest}"
        if new_stem == mp3.stem:
            continue
        tmp_path = mp3.with_name(mp3.name + ".reindex_tmp")
        if _rename_safe(mp3, tmp_path):
            temp_map.append((tmp_path, target_dir / (new_stem + ".mp3"), mp3.stem, new_stem))

    for tmp_path, final_path, old_stem, new_stem in temp_map:
        try:
            tmp_path.rename(final_path)
            changes.append({"old": old_stem + ".mp3", "new": new_stem + ".mp3"})
            for ext in (".mp4", ".webm", ".mkv"):
                ov = target_dir / (old_stem + ext)
                if ov.exists(): ov.rename(target_dir / (new_stem + ext))
            oc = cfg.covers_dir / (old_stem + ".jpg")
            if oc.exists(): oc.rename(cfg.covers_dir / (new_stem + ".jpg"))
        except Exception:
            pass

    invalidate_cache()
    return {"renamed": len(changes), "changes": changes}


def reindex_all() -> dict:
    results = {"root": reindex_folder("")}
    for sub in cfg.music_dir.iterdir():
        if sub.is_dir() and not sub.name.startswith('.'):
            results[sub.name] = reindex_folder(sub.name)
    total = sum(r["renamed"] for r in results.values())
    return {"total_renamed": total, "folders": results}
