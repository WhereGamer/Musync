"""Statistics and history management."""
import time
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

from .storage import load_json, save_json
from .config import cfg
from .tracks import split_artists

_hist_lock = __import__('threading').Lock()

def add_history(fn: str, pct: int, device: str = ""):
    with _hist_lock:
        h = load_json(cfg.hist_file, [])
        h.append({"fn": fn, "ts": int(time.time()), "pct": pct, "device": device})
        if len(h) > 50000:
            h = h[-50000:]
        save_json(cfg.hist_file, h)

def clear_history():
    save_json(cfg.hist_file, [])

def get_stats(device: str = "") -> dict:
    h_all = load_json(cfg.hist_file, [])
    h = [e for e in h_all if e.get("device") == device] if device else h_all
    if device and not h:
        h = h_all  # fallback

    from .tracks import get_tracks
    tracks = get_tracks()
    fn_map = {t["filename"]: t for t in tracks}

    now   = time.time()
    w_ago = now - 7  * 86400
    m_ago = now - 30 * 86400

    # Streak
    days_played = {datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d") for e in h}
    streak = 0
    d = datetime.now()
    while d.strftime("%Y-%m-%d") in days_played:
        streak += 1; d -= timedelta(days=1)

    # Max streak
    all_days = sorted(days_played)
    max_streak = cur_s = 1
    for i in range(1, len(all_days)):
        d1 = datetime.strptime(all_days[i-1], "%Y-%m-%d")
        d2 = datetime.strptime(all_days[i],   "%Y-%m-%d")
        cur_s = cur_s + 1 if (d2 - d1).days == 1 else 1
        max_streak = max(max_streak, cur_s)

    # Hourly heatmap
    hour_counts = [0] * 24
    for e in h:
        hour_counts[datetime.fromtimestamp(e["ts"]).hour] += 1
    fav_hour = hour_counts.index(max(hour_counts)) if any(hour_counts) else 0

    # Daily chart (30 days)
    daily = {}
    for e in h:
        if e["ts"] >= m_ago:
            day = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d")
            daily[day] = daily.get(day, 0) + 1
    daily_chart = [
        {"date": (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"),
         "count": daily.get((datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"), 0)}
        for i in range(29, -1, -1)
    ]
    best_day = max((d["count"] for d in daily_chart), default=0)

    month_h = [e for e in h if e["ts"] >= m_ago]
    week_h  = [e for e in h if e["ts"] >= w_ago]

    def top_tracks(entries, n=10):
        cnt = defaultdict(int)
        for e in entries:
            if e.get("pct", 100) >= 70:
                cnt[e["fn"]] += 1
        return [
            {"fn": fn, "count": c,
             "title": fn_map.get(fn, {}).get("title", fn),
             "artist": fn_map.get(fn, {}).get("artist", "")}
            for fn, c in sorted(cnt.items(), key=lambda x: -x[1])[:n]
        ]

    def top_artists(entries, n=10):
        cnt = defaultdict(int)
        for e in entries:
            t = fn_map.get(e["fn"])
            if t:
                for a in (t.get("artists") or [t.get("artist", "")]):
                    if a: cnt[a] += 1
        return sorted(cnt.items(), key=lambda x: -x[1])[:n]

    all_artists = set()
    for t in tracks:
        for a in (t.get("artists") or [t.get("artist", "")]):
            if a: all_artists.add(a)

    return {
        "total_plays":       len(h),
        "week_plays":        len(week_h),
        "month_plays":       len(month_h),
        "streak":            streak,
        "max_streak":        max_streak,
        "favorite_hour":     fav_hour,
        "best_day_count":    best_day,
        "hour_heatmap":      hour_counts,
        "daily_chart":       daily_chart,
        "top_tracks_week":   top_tracks(week_h),
        "top_tracks_month":  top_tracks(month_h),
        "top_artists_week":  top_artists(week_h),
        "top_artists_month": top_artists(month_h),
        "total_tracks":      len(tracks),
        "total_artists":     len(all_artists),
        "total_albums":      len({t["album"] for t in tracks if t.get("album")}),
    }

def get_yearly_stats(year: int) -> dict:
    h_all = load_json(cfg.hist_file, [])
    y0 = datetime(year, 1, 1).timestamp()
    y1 = datetime(year, 12, 31, 23, 59, 59).timestamp()
    year_h = [e for e in h_all if y0 <= e["ts"] <= y1]

    from .tracks import get_tracks
    tracks = get_tracks()
    fn_map = {t["filename"]: t for t in tracks}

    days_active = len({datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d") for e in year_h})
    minutes = sum(fn_map.get(e["fn"], {}).get("duration", 0) for e in year_h) // 60

    cnt_t = defaultdict(int)
    for e in year_h:
        if e.get("pct", 100) >= 70: cnt_t[e["fn"]] += 1
    top_tracks = [
        {"fn": fn, "count": c,
         "title": fn_map.get(fn, {}).get("title", fn),
         "artist": fn_map.get(fn, {}).get("artist", "")}
        for fn, c in sorted(cnt_t.items(), key=lambda x: -x[1])[:10]
    ]

    cnt_a = defaultdict(int)
    for e in year_h:
        t = fn_map.get(e["fn"])
        if t:
            for a in (t.get("artists") or [t.get("artist", "")]):
                if a: cnt_a[a] += 1
    top_artists = sorted(cnt_a.items(), key=lambda x: -x[1])[:10]

    monthly = [0] * 12
    for e in year_h:
        monthly[datetime.fromtimestamp(e["ts"]).month - 1] += 1

    available = sorted({datetime.fromtimestamp(e["ts"]).year for e in h_all}, reverse=True)

    return {
        "year": year, "total_plays": len(year_h),
        "days_active": days_active, "minutes": minutes,
        "top_tracks": top_tracks,
        "top_artists": [[a, c] for a, c in top_artists],
        "monthly": monthly,
        "best_month": monthly.index(max(monthly)) if monthly else 0,
        "available_years": available,
    }
