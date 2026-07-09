"""Duplicate track detection."""
from .tracks import get_tracks, normalize_title

def find_duplicates() -> list:
    tracks = get_tracks()
    groups = {}
    for t in tracks:
        key = normalize_title(t["title"]) + "|" + (t.get("artist") or "").lower()[:20]
        groups.setdefault(key, []).append(t)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    return [
        [{"filename": t["filename"], "title": t["title"],
          "artist": t["artist"], "duration": t["duration"],
          "playlist_folder": t.get("playlist_folder", "")} for t in group]
        for group in dups.values()
    ]
