#!/usr/bin/env python3
"""
MUSYNC Sync — playlist/track downloader (Python replacement for musync.sh)

Usage:
    python3 musync_sync.py "URL" [options]

Options:
    --from N        Start from track number N        (default: 1)
    --to N          Stop at track number N            (default: unlimited)
    --dir PATH      Music library root                (default: ~/storage/music/tracks)
    --jobs N        Parallel downloads                 (default: 3)
    --auto          Don't prompt on errors
    --clean         Remove tracks no longer in source playlist
    --mobile        Lower quality (128k) for faster sync
    --retry N       Max retry attempts per track       (default: 5)
    --fast          jobs=5, less politeness delay
    --safe          jobs=2, more politeness delay

Behaviour:
    • Playlist URL  → creates tracks/<Playlist Name>/, downloads there,
                      and auto-creates a matching MUSYNC playlist
                      (same name, description, cover).
    • Single track  → downloads directly into tracks/ (no subfolder).
    • Before any download, the WHOLE library (root + every playlist
      folder) is checked for an existing copy of the track. If found,
      it is reused instead of downloading a duplicate.
"""

import sys
import time
import shutil
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from musync_core.config import cfg
from musync_core.sync import Syncer

# ── COLORS ──────────────────────────────────────────────
class C:
    R = '\033[0;31m'; G = '\033[0;32m'; Y = '\033[0;33m'
    CY = '\033[0;36m'; W = '\033[1;37m'; D = '\033[2;37m'
    P = '\033[0;35m'; NC = '\033[0m'
    if not sys.stdout.isatty():
        R=G=Y=CY=W=D=P=NC=''

def ok(msg):   print(f"  {C.G}✓{C.NC}  {msg}")
def warn(msg): print(f"  {C.Y}!{C.NC}  {msg}")
def err(msg):  print(f"  {C.R}✗{C.NC}  {msg}")
def info(msg): print(f"  {C.CY}▸{C.NC}  {msg}")


def check_deps():
    missing = []
    for tool in ("yt-dlp", "ffmpeg"):
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        err(f"Missing: {', '.join(missing)}")
        warn(f"Install: pkg install {' '.join(missing)} -y")
        sys.exit(1)

    import subprocess
    try:
        v = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip()
        ok(f"yt-dlp {v}")
    except Exception:
        pass
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        if r.returncode == 0:
            ver_line = r.stdout.splitlines()[0]
            ok(f"ffmpeg ({ver_line.split()[2] if len(ver_line.split())>2 else 'ok'})")
        else:
            warn("ffmpeg found but not working — try: pkg upgrade -y --fix-missing")
    except Exception:
        pass


def acquire_lock() -> Path:
    lock = cfg.music_dir / ".musync_lock"
    try:
        lock.mkdir(parents=False)
    except FileExistsError:
        pid_file = lock / "pid"
        if pid_file.exists():
            try:
                old_pid = int(pid_file.read_text().strip())
                import os
                os.kill(old_pid, 0)  # raises if process doesn't exist
                err(f"MUSYNC already running (PID {old_pid})")
                err(f"Remove {lock} if this is stale")
                sys.exit(1)
            except (ProcessLookupError, ValueError, FileNotFoundError):
                pass
        shutil.rmtree(lock, ignore_errors=True)
        lock.mkdir(parents=False)
    import os
    (lock / "pid").write_text(str(os.getpid()))
    return lock


def print_header(args):
    w = 50
    print()
    print(f"{C.CY}  ╔{'═'*w}╗{C.NC}")
    title = "♫  MUSYNC  v10.0  (python)"
    print(f"{C.CY}  ║{C.NC}  {C.W}{title}{C.NC}{' '*(w-len(title)-2)}{C.CY}║{C.NC}")
    print(f"{C.CY}  ╠{'═'*w}╣{C.NC}")
    lines = [
        f"dir    : {cfg.music_dir.name}",
        f"range  : #{args.from_:03d} → #{args.to}",
        f"jobs   : {args.jobs}",
        f"quality: {'128k mobile' if args.mobile else 'max'}",
        f"retry  : max {args.retry}",
    ]
    for l in lines:
        print(f"{C.CY}  ║{C.NC}  {l}{' '*(w-len(l)-2)}{C.CY}║{C.NC}")
    print(f"{C.CY}  ╚{'═'*w}╝{C.NC}")
    print()


def print_summary(p):
    w = 50
    print()
    print(f"{C.CY}  ╔{'═'*w}╗{C.NC}")
    print(f"{C.CY}  ║{C.NC}  {C.W}DONE{C.NC}{' '*(w-6)}{C.CY}║{C.NC}")
    print(f"{C.CY}  ╠{'═'*w}╣{C.NC}")
    rows = [
        (f"{C.G}↓{C.NC}  downloaded  {p.downloaded}", ),
        (f"{C.P}✎{C.NC}  reused      {p.skipped}", ),
        (f"{C.R}✗{C.NC}  errors      {p.errors}", ),
        (f"{C.Y}↺{C.NC}  retries     {p.retries}", ),
    ]
    for (txt,) in rows:
        # account for ANSI codes when padding — rough estimate
        visible_len = len(txt) - txt.count('\033[0;32m')*5 - txt.count('\033[0;35m')*5 \
                      - txt.count('\033[0;31m')*5 - txt.count('\033[0;33m')*5 \
                      - txt.count('\033[0m')*4
        pad = max(0, w - visible_len - 2)
        print(f"{C.CY}  ║{C.NC}  {txt}{' '*pad}{C.CY}║{C.NC}")
    print(f"{C.CY}  ╚{'═'*w}╝{C.NC}")
    print()


def main():
    ap = argparse.ArgumentParser(description="MUSYNC playlist/track downloader")
    ap.add_argument("url", help="YouTube Music playlist or track URL")
    ap.add_argument("--from", dest="from_", type=int, default=1)
    ap.add_argument("--to", type=int, default=99999)
    ap.add_argument("--dir", type=str, default=None)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--mobile", action="store_true")
    ap.add_argument("--retry", type=int, default=5)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--safe", action="store_true")
    args = ap.parse_args()

    if args.dir:
        cfg.music_dir = Path(args.dir).expanduser()

    if args.fast:
        args.jobs = 5
    if args.safe:
        args.jobs = 2

    lock = acquire_lock()
    try:
        print_header(args)
        check_deps()

        info("Starting sync...")
        syncer = Syncer(
            url=args.url,
            start_from=args.from_,
            end_at=args.to,
            jobs=args.jobs,
            auto=args.auto,
            clean=args.clean,
            mobile=args.mobile,
            retry_max=args.retry,
        )

        # Run synchronously, streaming log lines as they appear
        import threading
        t = threading.Thread(target=syncer.run, daemon=True)
        t.start()

        printed = 0
        while t.is_alive() or printed < len(syncer.progress.log):
            log = syncer.progress.log
            while printed < len(log):
                line = log[printed]
                printed += 1
                if line.startswith("  ↓"):
                    print(f"{C.G}{line}{C.NC}")
                elif line.startswith("  ✗") or "[ERROR]" in line:
                    print(f"{C.R}{line}{C.NC}")
                elif line.startswith("  !"):
                    print(f"{C.Y}{line}{C.NC}")
                elif line.startswith("  =") or line.startswith("  ↺"):
                    print(f"{C.D}{line}{C.NC}")
                else:
                    print(f"{C.CY}{line}{C.NC}" if line.startswith("[MUSYNC]") else line)
            time.sleep(0.2)
        t.join()

        print_summary(syncer.progress)

    finally:
        shutil.rmtree(lock, ignore_errors=True)


if __name__ == "__main__":
    main()
