"""Configuration and paths."""
import os
from pathlib import Path
import platform
import json

_OS = platform.system()

def get_default_music_dir() -> Path:
    if _OS == "Windows":
        return Path.home() / "Music" / "tracks"
    return Path.home() / "storage" / "music" / "tracks"

class Config:
    def __init__(self):
        self._music_dir = get_default_music_dir()
        self._settings_file = None

    @property
    def music_dir(self) -> Path:
        return self._music_dir

    @music_dir.setter
    def music_dir(self, path):
        self._music_dir = Path(path).expanduser()
        self._music_dir.mkdir(parents=True, exist_ok=True)
        self._refresh_paths()

    def _refresh_paths(self):
        d = self._music_dir
        self.data_dir    = d / ".musync_data"
        self.lyrics_dir  = self.data_dir / "lyrics"
        self.covers_dir  = self.data_dir / "covers"
        self.meta_file   = self.data_dir / "meta.json"
        self.hist_file   = self.data_dir / "history.json"
        self.plists_file = self.data_dir / "playlists.json"
        self.aliases_file= self.data_dir / "artist_aliases.json"
        self.queue_file  = self.data_dir / "queue.json"
        self.settings_file = self.data_dir / "settings.json"
        self.color_cache_file = self.data_dir / "color_cache.json"
        self.theme_css_file = self.data_dir / "custom_theme.css"
        for d_ in (self.data_dir, self.lyrics_dir, self.covers_dir):
            d_.mkdir(parents=True, exist_ok=True)

    def load_settings(self):
        try:
            s = json.loads(self.settings_file.read_text("utf-8"))
            if "music_dir" in s:
                self._music_dir = Path(s["music_dir"]).expanduser()
                self._refresh_paths()
            return s
        except Exception:
            return {}

    def save_settings(self, data: dict):
        try:
            existing = self.load_settings()
            existing.update(data)
            self.settings_file.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception:
            pass

cfg = Config()
cfg._refresh_paths()

def reload_config():
    cfg.load_settings()
