"""
Cover color extraction and thumbnail normalization.

Key fix vs previous version: get_dominant_color() now guards against
B&W / near-monochrome covers where the saturation divisor is zero or
very small, which previously caused a negative hex value and ValueError.
"""
import io
import threading
from pathlib import Path
from .storage import load_json, save_json
from .config import cfg

try:
    from PIL import Image
    PIL = True
except ImportError:
    PIL = False

THUMB_SIZE_DEFAULT = 300
_color_cache_lock  = threading.Lock()


# ── Dominant color ────────────────────────────────────────
def get_dominant_color(img_data: bytes) -> str:
    """
    Return a vibrant hex accent color for a cover image.

    B&W / near-monochrome guard: if the max channel is 0 (pure black
    pixel) or saturation is effectively zero we skip that pixel entirely
    rather than dividing by zero or going negative.  The function now
    always returns a safe hex string or "".
    """
    if not PIL or not img_data:
        return ""
    try:
        img = Image.open(io.BytesIO(img_data)).convert("RGB").resize((30, 30))
        pixels = list(img.getdata())

        weighted = []
        for p in pixels:
            mn, mx = min(p), max(p)
            if mx == 0:            # pure black — skip
                continue
            sat = (mx - mn) / mx
            weighted.append((p, sat))

        if not weighted:
            return ""

        total_w = sum(w for _, w in weighted) or 1
        r = int(sum(p[0] * w for p, w in weighted) / total_w)
        g = int(sum(p[1] * w for p, w in weighted) / total_w)
        b = int(sum(p[2] * w for p, w in weighted) / total_w)
        # Boost saturation slightly for readability as a UI accent
        f = 1.4
        r = min(255, max(0, int(128 + (r - 128) * f)))
        g = min(255, max(0, int(128 + (g - 128) * f)))
        b = min(255, max(0, int(128 + (b - 128) * f)))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return ""


# ── Persistent color cache (thread-safe) ─────────────────
def get_cached_color(stem: str) -> str:
    with _color_cache_lock:
        cache = load_json(cfg.color_cache_file, {})
    return cache.get(stem, "")


def set_cached_color(stem: str, color: str):
    with _color_cache_lock:
        cache = load_json(cfg.color_cache_file, {})
        cache[stem] = color
        save_json(cfg.color_cache_file, cache)


def invalidate_color(stem: str):
    with _color_cache_lock:
        cache = load_json(cfg.color_cache_file, {})
        cache.pop(stem, None)
        save_json(cfg.color_cache_file, cache)


# ── Thumbnail cache (disk, one file per stem+size) ───────
def _thumbs_dir() -> Path:
    d = cfg.data_dir / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_thumbnail(stem: str, raw_data: bytes, size: int = THUMB_SIZE_DEFAULT) -> bytes:
    """
    Return a square JPEG of exactly `size`×`size`.

    Uses LANCZOS resampling in BOTH directions — upscaling small
    covers and downscaling large ones through the same code path —
    so every album/artist tile in the grid is the same size
    regardless of the original cover resolution.
    Cached to disk after first computation (instant on repeat requests).
    """
    cache_path = _thumbs_dir() / f"{stem}_{size}.jpg"
    if cache_path.exists():
        try:
            return cache_path.read_bytes()
        except Exception:
            pass

    if not PIL or not raw_data:
        return raw_data

    try:
        img = Image.open(io.BytesIO(raw_data)).convert("RGB")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top  = (h - side) // 2
        img  = img.crop((left, top, left + side, top + side))
        img  = img.resize((size, size), Image.LANCZOS)
        buf  = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        data = buf.getvalue()
        try:
            cache_path.write_bytes(data)
        except Exception:
            pass
        return data
    except Exception:
        return raw_data


def invalidate_thumbnails(stem: str):
    """Remove all cached thumbnails for a stem (called on cover upload)."""
    for f in _thumbs_dir().glob(f"{stem}_*.jpg"):
        try:
            f.unlink()
        except Exception:
            pass
