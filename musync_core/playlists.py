"""Playlist management, including liked tracks."""
from pathlib import Path
from .storage import load_json, save_json
from .config import cfg

LIKED_ID = "__liked__"

def get_all() -> dict:
    pls = load_json(cfg.plists_file, {})
    if LIKED_ID not in pls:
        pls[LIKED_ID] = {"name": "❤ Liked", "tracks": [], "protected": True}
        save_json(cfg.plists_file, pls)
    return pls

def save_all(data: dict):
    save_json(cfg.plists_file, data)

def toggle_liked(fn: str) -> dict:
    pls = get_all()
    liked = pls[LIKED_ID]["tracks"]
    if fn in liked:
        liked.remove(fn); state = False
    else:
        liked.insert(0, fn); state = True
    save_json(cfg.plists_file, pls)
    return {"liked": state, "count": len(liked)}

def is_liked(fn: str) -> bool:
    pls = load_json(cfg.plists_file, {})
    return fn in pls.get(LIKED_ID, {}).get("tracks", [])

def edit_playlist(pl_id: str, data: dict) -> bool:
    pls = get_all()
    if pl_id not in pls:
        return False
    pl = pls[pl_id]
    if pl.get("protected") and pl_id == LIKED_ID:
        if "tracks" in data:
            pl["tracks"] = data["tracks"]
    else:
        for k in ("name", "description", "tracks"):
            if k in data: pl[k] = data[k]
    save_json(cfg.plists_file, pls)
    return True

def save_cover(pl_id: str, file_data, filename: str = "cover.jpg") -> str:
    dest = cfg.data_dir / f"pl_cover_{pl_id}.jpg"
    file_data.save(str(dest))
    pls = get_all()
    if pl_id in pls:
        pls[pl_id]["cover"] = str(dest)
        save_json(cfg.plists_file, pls)
    return str(dest)

def get_cover_path(pl_id: str):
    pls = load_json(cfg.plists_file, {})
    cover = pls.get(pl_id, {}).get("cover", "")
    if cover and Path(cover).exists():
        return cover
    return None

def create_or_get_by_name(name: str, description: str = "", cover_url: str = "") -> str:
    """Find playlist by name, or create new one. Returns playlist ID."""
    pls = get_all()
    for pid, pl in pls.items():
        if pl.get("name", "").strip().lower() == name.strip().lower():
            return pid
    pid = f"pl_sync_{abs(hash(name)) % 100000}"
    pls[pid] = {
        "name": name,
        "description": description,
        "tracks": [],
        "cover_url": cover_url,
        "synced": True,  # marks this as an auto-created sync playlist
    }
    save_json(cfg.plists_file, pls)
    return pid

def add_track_to_playlist(pl_id: str, filename: str):
    pls = get_all()
    if pl_id in pls and filename not in pls[pl_id]["tracks"]:
        pls[pl_id]["tracks"].append(filename)
        save_json(cfg.plists_file, pls)


# ════════════════════════════════════════════════════════
#  REAL FILE COUNT — playlist.tracks list can drift from
#  what's actually on disk (sync skips, manual deletes, etc).
#  This counts the ACTUAL .mp3 files in the playlist's folder.
# ════════════════════════════════════════════════════════
def _safe_folder_name(name: str) -> str:
    import re
    s = re.sub(r'[/\\:*?"<>|]', '-', name)
    return re.sub(r'\s+', ' ', s).strip()[:180]

def get_all_with_counts() -> dict:
    """Same as get_all(), but each playlist gets a real 'file_count'
    based on actual .mp3 files in its folder (falls back to
    len(tracks) if no matching folder exists, e.g. manually-built
    playlists that pull from multiple folders/root)."""
    pls = get_all()
    for pid, pl in pls.items():
        folder = cfg.music_dir / _safe_folder_name(pl.get("name", ""))
        if folder.is_dir():
            pl["file_count"] = len(list(folder.glob("*.mp3")))
        else:
            pl["file_count"] = len(pl.get("tracks", []))
    return pls
