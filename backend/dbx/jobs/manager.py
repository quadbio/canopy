"""Job scheduling in the server: queue, start worker processes, watch them, cancel.

A job runs in its own process (`python -m dbx.jobs.worker <job> <config>`, in its own session so
a Ctrl-C in the server's terminal does not reach it), so a conversion that needs a lot of memory
or crashes never takes the server down, and it logs to its own file (`dbx-worker-<pid>.jsonl`).

Every second the scheduler:
- reaps finished workers (a worker that died without recording an outcome fails the job, unless
  the server had asked it to stop);
- marks running jobs whose worker vanished or stopped sending heartbeats `interrupted`
  (also workers left by an earlier server);
- starts at most one queued job, if fewer than `jobs.max_running` run **and** the memory guard
  allows: the host (and its cgroup) must have 1.5 × `jobs.memory_gb` plus the job's estimated
  cell-table memory available; otherwise the job waits (stage `waiting_for_memory`, with the
  numbers in `info`).

One server per cache folder (`server.lock`): two servers would start each other's jobs twice.
Temporary stores and spill files of jobs that are not running are removed at start and before
each launch, whatever ended them (also a kill that left no chance to clean up).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dbx.config import Config
from dbx.jobs.db import ACTIVE, DB_FILE, Job, JobsDB
from dbx.logs import get_logger, memory_headroom

GB = 2**30
_LOCKS: dict[str, Any] = {}  # cache folders this process holds the lock of


class CacheInUseError(RuntimeError):
    """Another server process uses this cache folder."""


def lock_cache(cache_dir: Path) -> None:
    """Take the cache's server lock for this process (re-entrant within the process)."""
    key = str(cache_dir.resolve())
    if key in _LOCKS:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = open(cache_dir / "server.lock", "a+")  # noqa: SIM115 (held for the process lifetime)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        f.close()
        raise CacheInUseError(
            f"another dbx server uses the cache folder {cache_dir}; stop it or use another "
            "cache_dir"
        ) from e
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _LOCKS[key] = f


def tmp_dirs(cache_dir: Path, tmp_dir: Path | None) -> list[Path]:
    """Every temporary store, spill folder and replaced store in the cache."""
    out = list(cache_dir.glob("*/c*.tmp-*")) + list(cache_dir.glob("*/c*.old-*"))
    spill = tmp_dir or cache_dir / "tmp"
    if spill.exists():
        out += list(spill.glob("c*.tmp-*"))
    return out


def job_of(path: Path) -> int | None:
    """The job id in a temporary folder's name (`c0.tmp-j<job>-<random>`)."""
    part = path.name.split(".tmp-j", 1)
    if len(part) == 2 and part[1].split("-", 1)[0].isdigit():
        return int(part[1].split("-", 1)[0])
    return None


class JobManager:
    def __init__(
        self,
        cfg: Config,
        *,
        wal: bool,
        on_done: Callable[[Job], None] | None = None,
    ) -> None:
        self.cfg = cfg
        lock_cache(cfg.cache_dir)
        self.db = JobsDB(cfg.cache_dir / DB_FILE, wal=wal)
        self.on_done = on_done
        self.log = get_logger("dbx.jobs")
        self.procs: dict[int, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jobs", daemon=True)
        for id in self.db.mark_interrupted():
            self.log.warning("job_interrupted", job=id)
        for id in self.db.requeue_stopped():
            self.log.info("job_requeued", job=id, reason="server_stopped")
        self.clean_tmp()

    # ------------------------------------------------------------ API
    def submit(
        self, kind: str, dataset: str, version: str, options: dict[str, Any], source: Path, fp: str
    ) -> Job:
        """A new job, or the active one of this dataset version (whatever its options: one
        conversion per dataset at a time; the caller compares options)."""
        with self._lock:
            for j in self.db.find(dataset=dataset, states=ACTIVE):
                if j.version == version and j.kind == kind:
                    return j
            id = self.db.add(kind, dataset, version, options, str(source), fp)
        self.log.info("job_queued", job=id, kind=kind, dataset_id=dataset, options=options)
        self._wake.set()
        job = self.db.get(id)
        assert job is not None
        return job

    def cancel(self, id: int) -> Job | None:
        return self._stop_job(id, "cancelled")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        with self._lock:
            procs = list(self.procs.items())
        for id, _ in procs:  # stopped now, run again at the next start
            self._stop_job(id, "server_stopped")
        for id, p in procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()  # its temporary files are removed at the next start
                self.log.warning("job_killed_on_shutdown", job=id)

    def tick(self) -> None:
        """One scheduling round (also called directly by tests)."""
        self._reap()
        for id in self.db.mark_interrupted():
            self.log.warning("job_interrupted", job=id)
        self._start_queued()

    def clean_tmp(self) -> None:
        """Remove temporary stores and spill folders of jobs that are not running."""
        running = {j.id for j in self.db.find(states=("running",)) if j.process_alive()}
        for path in tmp_dirs(self.cfg.cache_dir, self.cfg.jobs.tmp_dir):
            job = job_of(path)
            if job is not None and job in running:
                continue
            size = _size(path)
            shutil.rmtree(path, ignore_errors=True)
            self.log.info("tmp_removed", job=job, bytes=size)

    # ------------------------------------------------------------ internals
    def _stop_job(self, id: int, reason: str) -> Job | None:
        job = self.db.get(id)
        if job is None or job.state not in ACTIVE:
            return job
        with self._lock:
            proc = self.procs.get(id)
        if job.state == "queued" and (proc is None or proc.poll() is not None):
            state = "cancelled" if reason == "cancelled" else "queued"
            if state == "cancelled":
                self.db.update(id, state="cancelled", finished=time.time(), stage=None)
                self.log.info("job_cancelled", job=id, while_state="queued")
            return self.db.get(id)
        # The worker reads the reason when it stops (cancelled, or interrupted for a restart).
        self.db.update(id, stop_reason=reason)
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        elif job.process_alive() and job.pid is not None:  # a worker of an earlier server
            os.kill(job.pid, signal.SIGTERM)
        self.log.info("job_stop_requested", job=id, reason=reason)
        return self.db.get(id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # the scheduler must never die
                self.log.warning("jobs_tick_failed", error=type(e).__name__)
            self._wake.wait(1.0)
            self._wake.clear()

    def _reap(self) -> None:
        with self._lock:
            done = [(id, p) for id, p in self.procs.items() if p.poll() is not None]
            for id, _ in done:
                del self.procs[id]
        for id, p in done:
            job = self.db.get(id)
            if job is None:
                continue
            if job.state in ACTIVE:  # the worker ended without recording an outcome
                code = p.returncode
                if job.stop_reason == "cancelled":
                    state, error = "cancelled", "cancelled"
                elif job.stop_reason == "server_stopped":
                    state, error = "interrupted", "server_stopped"
                else:
                    state = "failed"
                    error = f"worker_killed:{-code}" if code < 0 else f"worker_exit:{code}"
                self.db.update(id, state=state, error=error, finished=time.time())
                self.log.error("job_worker_ended", job=id, returncode=code, state=state)
                job = self.db.get(id)
                self.clean_tmp()
            assert job is not None
            self.log.info(
                "job_finished",
                job=id,
                state=job.state,
                error=job.error,
                seconds=round((job.finished or time.time()) - (job.started or job.created), 1),
            )
            if job.state == "done" and self.on_done:
                self.on_done(job)

    def _start_queued(self) -> None:
        with self._lock:
            running = len(self.procs)
        if running >= self.cfg.jobs.max_running:
            return
        launched = set(self.procs)
        queued = [j for j in sorted(self.db.find(states=("queued",)), key=lambda j: j.id)]
        job = next((j for j in queued if j.id not in launched), None)
        if job is None:
            return
        need = int(1.5 * self.cfg.jobs.memory_gb * GB) + table_bytes(job)
        available = memory_headroom()
        if available < need:
            info = {"need_bytes": need, "available_bytes": available}
            if job.stage != "waiting_for_memory":
                self.log.warning("job_waiting_for_memory", job=job.id, **info)
            self.db.update(job.id, stage="waiting_for_memory", info=info)
            return
        self.clean_tmp()
        self._launch(job)

    def _launch(self, job: Job) -> None:
        # The worker gets this server's configuration in a file of its own (written atomically).
        conf = self.cfg.cache_dir / "jobs" / f"job-{job.id}.json"
        conf.parent.mkdir(exist_ok=True)
        part = conf.with_suffix(".part")
        part.write_text(self.cfg.model_dump_json())
        part.replace(conf)
        cmd = [sys.executable, "-m", "dbx.jobs.worker", str(job.id), str(conf)]
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(  # noqa: S603 (our own interpreter and module, no shell)
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,
            start_new_session=True,
        )
        with self._lock:
            self.procs[job.id] = proc
        self.db.update(job.id, stage=None, info=None)
        self.log.info("job_started", job=job.id, worker_pid=proc.pid, kind=job.kind)


def table_bytes(job: Job) -> int:
    """Estimated memory for the job's cell and gene tables (read whole by the converter)."""
    from dbx.convert.anndata import table_memory_estimate
    from dbx.inspect.models import Fingerprint

    try:
        fp = Fingerprint.model_validate_json(job.fingerprint)
    except ValueError:
        return 0
    return table_memory_estimate(fp.anndata) if fp.anndata else 0


def _size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / f).stat().st_size
    return total
