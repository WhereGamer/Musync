"""Lyrics fetching and storage.

Sources are tried in order, cheapest/most-authoritative first:

  1. Locally saved .lrc / .txt — either pasted by hand, or auto-cached here
     the first time a remote source finds a match (see get_lyrics()).
  2. Better Lyrics API (https://lyrics-api.boidu.dev) — free, open source,
     cache-first. This is the same backend the "Better Lyrics" browser
     extension uses, and it already aggregates Musixmatch, YouTube,
     YouTube Captions, Better Lyrics Unison/Legato/Portato and BiniLyrics
     upstream, returning syllable/word-level TTML timing when a cached
     match exists. Uncached (brand new) lookups need an API key we don't
     have, so those just fall through to LRCLib below — this is expected,
     not an error. Attribution: https://better-lyrics.boidu.dev
  3. LRCLib (https://lrclib.net) — free, open, no key required, line-level
     only. Good fallback for anything Better Lyrics hasn't seen yet.
  4. Nothing found -> empty result; the user can still paste lyrics by hand
     in the edit sheet.

Data shape returned by get_lyrics() / parse_lrc() / parse_ttml():
  {
    "synced": bool,
    "source": "local" | "better-lyrics" | "lrclib" | "manual" | "none",
    "plain":  str,          # raw text, used as a fallback / for editing
    "lines": [
      {
        "time": float,           # line start, seconds
        "end":  float | None,    # line end, seconds (when known)
        "text": str,
        "words": [               # None when only line-level timing exists
          {"time": float, "end": float, "text": str}, ...
        ] | None
      }, ...
    ]
  }

Word-level timing is persisted locally using "enhanced" LRC (the same
`<mm:ss.xx>word ` tag format Musixmatch/MiniLyrics/foobar2000 use), so a
saved file stays a single plain-text, human-editable/portable .lrc — no
extra sidecar file, and anything already in that format that a user pastes
in gets word-level karaoke for free too.
"""
import re
from pathlib import Path
from typing import Optional
from .config import cfg

try:
    import requests as rq
    REQUESTS = True
except ImportError:
    REQUESTS = False

try:
    import xml.etree.ElementTree as ET
except ImportError:
    ET = None

BETTER_LYRICS_URL = "https://lyrics-api.boidu.dev/getLyrics"
LRCLIB_URL = "https://lrclib.net/api/get"
MIN_SCORE = 40  # Better Lyrics match confidence (0-100) below which we skip it
TTML_NS = "{http://www.w3.org/ns/ttml}"
WORD_TAG_RE = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>")


def lrc_file(fn: str) -> Path:
    return cfg.lyrics_dir / (Path(fn).stem + ".lrc")

def txt_file(fn: str) -> Path:
    return cfg.lyrics_dir / (Path(fn).stem + ".txt")


# ── LRC (+ enhanced/word-level) parsing & writing ──────────────────────

def _fmt_lrc_time(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return f"{m:02d}:{s:05.2f}"

def parse_lrc(lrc: str, source: str) -> dict:
    """Parse plain LRC ([mm:ss.xx]text) or enhanced/word-level LRC
    ([mm:ss.xx]<mm:ss.xx>word <mm:ss.xx>word ...) into the common shape."""
    lines = []
    for line in lrc.splitlines():
        if not (line.startswith("[") and "]" in line):
            continue
        tp   = line[1:line.index("]")]
        rest = line[line.index("]") + 1:]
        try:
            m, s = tp.split(":")
            t0 = float(m) * 60 + float(s)
        except Exception:
            continue
        tags = list(WORD_TAG_RE.finditer(rest))
        if tags:
            words = []
            for i, wm in enumerate(tags):
                wt = float(wm.group(1)) * 60 + float(wm.group(2))
                start = wm.end()
                end = tags[i + 1].start() if i + 1 < len(tags) else len(rest)
                txt = rest[start:end]
                if txt:
                    words.append({"time": wt, "end": wt, "text": txt})
            for i in range(len(words) - 1):
                words[i]["end"] = words[i + 1]["time"]
            lines.append({"time": t0, "end": None, "text": "".join(w["text"] for w in words).strip(), "words": words})
        else:
            lines.append({"time": t0, "end": None, "text": rest.strip(), "words": None})
    if lines:
        return {"synced": True, "lines": lines, "plain": lrc, "source": source}
    return {"synced": False, "lines": [], "plain": lrc, "source": source}

def to_lrc(result: dict) -> str:
    """Serialize a synced result back to (enhanced) LRC text for local caching."""
    out = []
    for ln in result.get("lines", []):
        head = f"[{_fmt_lrc_time(ln['time'])}]"
        words = ln.get("words")
        if words:
            out.append(head + "".join(f"<{_fmt_lrc_time(w['time'])}>{w['text']}" for w in words))
        else:
            out.append(head + (ln.get("text") or ""))
    return "\n".join(out)


# ── Better Lyrics (TTML, word/syllable-level) ──────────────────────────

def _ttml_time(s: str) -> float:
    parts = (s or "0").split(":")
    try:
        if len(parts) == 3: return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2: return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except Exception:
        return 0.0

def parse_ttml(ttml: str, source: str) -> dict:
    if not ET:
        return {"synced": False, "lines": [], "plain": "", "source": source}
    try:
        root = ET.fromstring(ttml)
    except ET.ParseError:
        return {"synced": False, "lines": [], "plain": "", "source": source}

    lines, plain_parts = [], []
    for p in root.iter(f"{TTML_NS}p"):
        begin_raw = p.get("begin")
        if not begin_raw:
            continue
        words = []
        def walk(el):
            for child in el:
                if not child.tag.endswith("}span"):
                    continue
                b = child.get("begin")
                if b:
                    txt = "".join(child.itertext())
                    if txt:
                        words.append({"time": _ttml_time(b), "end": _ttml_time(child.get("end") or b), "text": txt})
                else:
                    walk(child)  # wrapper span (e.g. background vocals)
        walk(p)
        text = "".join(w["text"] for w in words).strip()
        if not text:
            continue
        end_raw = p.get("end")
        lines.append({"time": _ttml_time(begin_raw), "end": _ttml_time(end_raw) if end_raw else None,
                       "text": text, "words": words or None})
        plain_parts.append(text)

    if not lines:
        return {"synced": False, "lines": [], "plain": "", "source": source}
    return {"synced": True, "lines": lines, "plain": "\n".join(plain_parts), "source": source}

def _from_better_lyrics(title: str, artist: str, album: str = "", duration=0):
    if not (REQUESTS and title and artist):
        return None
    try:
        params = {"s": title.strip(), "a": artist.strip()}
        if album: params["al"] = album.strip()
        if duration: params["d"] = int(duration)
        r = rq.get(BETTER_LYRICS_URL, params=params, timeout=6,
                    headers={"User-Agent": "musync (personal local music player)"})
        if r.status_code != 200:
            return None  # cache miss (401 - no API key), not found (404), rate-limited (429): try next source
        d = r.json() or {}
        if d.get("score") is not None and d["score"] < MIN_SCORE:
            return None
        ttml = d.get("ttml", "")
        if not ttml:
            return None
        res = parse_ttml(ttml, "better-lyrics")
        return res if (res["synced"] or res["plain"]) else None
    except Exception:
        return None


# ── LRCLib (line-level, no key needed) ─────────────────────────────────

def _from_lrclib(title: str, artist: str, album: str = "", duration=0):
    if not (REQUESTS and (title or artist)):
        return None
    try:
        params = {"track_name": title, "artist_name": artist}
        if album: params["album_name"] = album
        if duration: params["duration"] = int(duration)
        r = rq.get(LRCLIB_URL, params=params, timeout=6)
        if r.status_code != 200:
            return None
        d = r.json()
        lrc, plain = d.get("syncedLyrics", ""), d.get("plainLyrics", "")
        if lrc:   return parse_lrc(lrc, "lrclib")
        if plain: return {"synced": False, "lines": [], "plain": plain, "source": "lrclib"}
    except Exception:
        pass
    return None


# ── Orchestration ───────────────────────────────────────────────────────
# Strategy pattern: each remote source is a small class implementing the
# same fetch() interface. get_lyrics() just walks the list in order and
# uses the first one that returns a result — if Better Lyrics is down or
# rate-limited, LRCLib is tried next with zero change to the calling code.
# Adding a new source later is just adding another class to REMOTE_SOURCES.

class LyricsProvider:
    name = "base"
    def fetch(self, title: str, artist: str, album: str = "", duration=0) -> Optional[dict]:
        raise NotImplementedError


class BetterLyricsProvider(LyricsProvider):
    name = "better-lyrics"
    def fetch(self, title, artist, album="", duration=0):
        return _from_better_lyrics(title, artist, album, duration)


class LRCLibProvider(LyricsProvider):
    name = "lrclib"
    def fetch(self, title, artist, album="", duration=0):
        return _from_lrclib(title, artist, album, duration)


REMOTE_SOURCES = (BetterLyricsProvider(), LRCLibProvider())


def _fetch_remote(title: str, artist: str, album: str = "", duration=0) -> Optional[dict]:
    for provider in REMOTE_SOURCES:
        try:
            res = provider.fetch(title, artist, album, duration)
        except Exception:
            res = None
        if res:
            return res
    return None


def _online_lrc_file(video_id: str) -> Path:
    return cfg.lyrics_dir / f"online_{video_id}.lrc"

def _online_txt_file(video_id: str) -> Path:
    return cfg.lyrics_dir / f"online_{video_id}.txt"


def get_lyrics(fn: str = "", title: str = "", artist: str = "", album: str = "",
                duration=0, video_id: str = "", auto_save: bool = True) -> dict:
    """
    fn        — local library filename (existing behaviour).
    video_id  — set instead of fn for a track being listened to online
                (not yet downloaded) so a match can still be cached and
                reused, including once the track IS downloaded later
                (see migrate_online_lyrics()).
    auto_save — if False, a found remote match is returned but NOT cached
                locally (respects the "auto-fetch lyrics" setting being
                off, while still letting someone open the lyrics panel
                and search manually).
    """
    empty = {"synced": False, "lines": [], "plain": "", "source": "none"}
    if fn:
        lf = lrc_file(fn)
        if lf.exists():
            return parse_lrc(lf.read_text("utf-8"), "local")
        tf = txt_file(fn)
        if tf.exists():
            return {"synced": False, "lines": [], "plain": tf.read_text("utf-8"), "source": "local"}
    elif video_id:
        lf = _online_lrc_file(video_id)
        if lf.exists():
            return parse_lrc(lf.read_text("utf-8"), "local")
        tf = _online_txt_file(video_id)
        if tf.exists():
            return {"synced": False, "lines": [], "plain": tf.read_text("utf-8"), "source": "local"}

    if title or artist:
        res = _fetch_remote(title, artist, album, duration)
        if res and auto_save:
            try:
                if fn:
                    if res["synced"]: save_lyrics(fn, to_lrc(res), True)
                    elif res.get("plain"): save_lyrics(fn, res["plain"], False)
                elif video_id:
                    save_online_lyrics(video_id, to_lrc(res) if res["synced"] else res.get("plain", ""), res["synced"])
            except Exception:
                pass
        if res:
            return res
    return empty


def save_online_lyrics(video_id: str, text: str, synced: bool):
    """Cache lyrics found while listening to a not-yet-downloaded track,
    keyed by its YouTube video_id so they survive across sessions and
    can be migrated onto the real file once it's downloaded."""
    if not text:
        return
    dest = _online_lrc_file(video_id) if synced else _online_txt_file(video_id)
    dest.write_text(text, "utf-8")
    other = _online_txt_file(video_id) if synced else _online_lrc_file(video_id)
    try: other.unlink()
    except Exception: pass


def migrate_online_lyrics(video_id: str, fn: str):
    """Called after a track finishes downloading: if lyrics were cached
    for it while it was being listened to online, attach them to the real
    file so they work fully offline, then drop the online-only copy."""
    for src, synced in ((_online_lrc_file(video_id), True), (_online_txt_file(video_id), False)):
        if src.exists():
            try:
                save_lyrics(fn, src.read_text("utf-8"), synced)
                src.unlink()
            except Exception:
                pass
            return


def save_lyrics(fn: str, text: str, synced: bool):
    dest = lrc_file(fn) if synced else txt_file(fn)
    dest.write_text(text, "utf-8")
    other = txt_file(fn) if synced else lrc_file(fn)
    try: other.unlink()  # don't leave a stale copy in the other format
    except Exception: pass
    _embed_id3_lyrics(fn, text, synced)

def delete_lyrics(fn: str):
    for f in (lrc_file(fn), txt_file(fn)):
        try: f.unlink()
        except Exception: pass


# ── ID3 embedding (SYLT/USLT) — makes the mp3 itself carry the lyrics,
# so they work fully offline even if .musync_data is ever lost/not synced
# to another device, e.g. copying just the mp3 elsewhere. ──────────────
def _embed_id3_lyrics(fn: str, text: str, synced: bool):
    try:
        from mutagen.id3 import ID3, USLT, SYLT, Encoding
    except Exception:
        return
    try:
        from .tracks import resolve_track_path
    except Exception:
        return
    path = resolve_track_path(fn)
    if not path or not path.exists():
        return
    try:
        try: tags = ID3(str(path))
        except Exception: tags = ID3()
        # Always keep a plain USLT (unsynced) copy — needed as a fallback
        # for players/tags that don't understand SYLT.
        plain = text
        if synced:
            res = parse_lrc(text, "local")
            plain = "\n".join(l["text"] for l in res.get("lines", [])) or text
        tags.setall("USLT", [USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=plain)])
        if synced:
            res = parse_lrc(text, "local")
            events = [(int(round(l["time"] * 1000)), l["text"]) for l in res.get("lines", [])]
            if events:
                tags.setall("SYLT", [SYLT(encoding=Encoding.UTF8, lang="eng", format=2,
                                           type=1, desc="", text=events)])
        else:
            tags.delall("SYLT")
        tags.save(str(path))
    except Exception:
        pass
