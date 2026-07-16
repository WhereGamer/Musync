"""
Task queue for yt-dlp downloads.

Replaces the old "spawn every download as a thread immediately, gate on a
semaphore" approach with a real priority queue plus a fixed worker pool,
so:
  - pending items can be reprioritized (jump the queue),
  - both pending and in-flight items can be individually paused/resumed
    or cancelled, without touching anything else in the sync.

Pause/resume of an already-running download uses SIGSTOP/SIGCONT on the
yt-dlp subprocess. That's POSIX-only (Linux, macOS, and Termux on
Android — all POSIX), which covers this project's actual deployment
targets. On Windows, a pause request is still honoured immediately for
anything still queued; an in-flight download will finish that attempt
and then stop before its next retry rather than freezing mid-download.
"""
import heapq
import itertools
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

IS_POSIX = os.name == "posix"


class Job:
    def __init__(self, job_id: str, run_fn: Callable, priority: int = 0, label: str = ""):
        self.id = job_id
        self.run_fn = run_fn                 # callable(job) -> None
        self.priority = priority             # higher = starts sooner
        self.label = label
        self.status = "queued"               # queued|downloading|paused|done|cancelled|error
        self.process: Optional[subprocess.Popen] = None
        self.cancelled = threading.Event()
        self.paused = threading.Event()
        self.error = None
        self.created = time.time()

    def request_pause(self):
        self.paused.set()
        if self.status == "downloading":
            self.status = "paused"
            if self.process and IS_POSIX:
                try: self.process.send_signal(signal.SIGSTOP)
                except Exception: pass
        elif self.status == "queued":
            self.status = "paused"

    def request_resume(self):
        self.paused.clear()
        if self.status == "paused":
            self.status = "downloading" if self.process else "queued"
        if self.process and IS_POSIX:
            try: self.process.send_signal(signal.SIGCONT)
            except Exception: pass

    def request_cancel(self):
        self.cancelled.set()
        self.paused.clear()
        self.status = "cancelled"
        if self.process:
            try:
                if IS_POSIX: self.process.send_signal(signal.SIGCONT)  # in case it was paused
                self.process.terminate()
            except Exception:
                pass

    def to_dict(self):
        return {"id": self.id, "label": self.label, "status": self.status,
                "priority": self.priority}


class DownloadManager:
    """One instance per running sync. Worker threads pull the
    highest-priority queued job and run it; priority/pause/cancel can be
    changed on any job at any time from another thread (e.g. an API
    request handler)."""

    def __init__(self, workers: int = 3):
        self.workers = max(1, workers)
        self._jobs: dict = {}
        self._pending: list = []              # heap of (-priority, seq, job_id)
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._shutdown = False
        self._threads = [threading.Thread(target=self._worker_loop, daemon=True)
                          for _ in range(self.workers)]
        for t in self._threads:
            t.start()

    def submit(self, job_id: str, run_fn: Callable, priority: int = 0, label: str = "") -> Job:
        job = Job(job_id, run_fn, priority, label)
        with self._cv:
            self._jobs[job_id] = job
            heapq.heappush(self._pending, (-priority, next(self._seq), job_id))
            self._cv.notify_all()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def set_priority(self, job_id: str, priority: int) -> bool:
        with self._cv:
            job = self._jobs.get(job_id)
            if not job or job.status not in ("queued", "paused"):
                return False
            job.priority = priority
            self._pending = [(-self._jobs[jid].priority, seq, jid)
                              for (_, seq, jid) in self._pending if jid in self._jobs]
            heapq.heapify(self._pending)
            self._cv.notify_all()
            return True

    def pause(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "downloading"):
            return False
        job.request_pause()
        with self._cv: self._cv.notify_all()
        return True

    def resume(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != "paused":
            return False
        job.request_resume()
        with self._cv:
            heapq.heappush(self._pending, (-job.priority, next(self._seq), job_id))
            self._cv.notify_all()
        return True

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status in ("done", "cancelled"):
            return False
        job.request_cancel()
        with self._cv: self._cv.notify_all()
        return True

    def status(self) -> list:
        return [j.to_dict() for j in list(self._jobs.values())]

    def shutdown(self):
        self._shutdown = True
        with self._cv: self._cv.notify_all()

    def _worker_loop(self):
        while not self._shutdown:
            job_id = None
            with self._cv:
                while not self._shutdown:
                    while self._pending:
                        _, _, jid = heapq.heappop(self._pending)
                        j = self._jobs.get(jid)
                        if not j or j.cancelled.is_set():
                            continue
                        if j.status == "paused":
                            continue  # dropped; resume() re-enqueues it
                        job_id = jid
                        break
                    if job_id is not None:
                        break
                    self._cv.wait(timeout=1)
                if self._shutdown:
                    return
            job = self._jobs[job_id]
            if job.cancelled.is_set():
                continue
            job.status = "downloading"
            try:
                job.run_fn(job)
                if job.status not in ("cancelled",):
                    job.status = "done" if not job.cancelled.is_set() else "cancelled"
            except Exception as e:
                job.status = "error"
                job.error = str(e)


def run_popen(cmd: list, job: Job, timeout: int = 180):
    """Runs `cmd` via Popen (instead of subprocess.run) so the live
    process handle can be attached to `job`, letting pause()/cancel()
    reach it while it's running. Mimics subprocess.run's return value
    shape (.returncode/.stdout/.stderr) so callers don't need to change."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    job.process = proc
    if job.paused.is_set() and IS_POSIX:
        try: proc.send_signal(signal.SIGSTOP)
        except Exception: pass
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        job.process = None
        raise
    job.process = None

    class _Result:
        pass
    r = _Result()
    r.returncode = proc.returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


_managers: dict = {}
_managers_lock = threading.Lock()

def get_download_manager(workers: int = 3, key: str = "default") -> DownloadManager:
    """Shared manager per sync run (keyed so overlapping syncs, if the app
    ever allows them, don't fight over each other's queues)."""
    with _managers_lock:
        mgr = _managers.get(key)
        if mgr is None or mgr._shutdown:
            mgr = DownloadManager(workers=workers)
            _managers[key] = mgr
        return mgr
