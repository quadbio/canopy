"""Job scheduling in the server: queue, start worker processes, watch them, cancel.

A job runs in its own process (`python -m canopy.jobs.worker <job> <config>`, in its own session so
a Ctrl-C in the server's terminal does not reach it), so a conversion that needs a lot of memory
or crashes never takes the server down, and it logs to its own file (`canopy-worker-<pid>.jsonl`).

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
import errno
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

from canopy.config import Config
from canopy.jobs.db import ACTIVE, DB_FILE, Job, JobsDB
from canopy.logs import (
    cgroup_memory_limit,
    current_log_file,
    get_logger,
    memory_headroom,
    memory_total,
)

GB = 2**30
_LOCKS: dict[str, Any] = {}  # cache folders this process holds the lock of


class CacheInUseError(RuntimeError):
    """Another server process uses this cache folder."""


class QueueFullError(RuntimeError):
    """jobs.max_queued jobs wait already."""


class LockUnsupportedError(RuntimeError):
    """File locks do not work where the state folder is (some network file systems)."""


def lock_cache(cache_dir: Path) -> None:
    """Take the server lock in this folder for this process (re-entrant within the process).
    Only "would block" means another server holds it; other errors mean locks do not work there."""
    key = str(cache_dir.resolve())
    if key in _LOCKS:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = open(cache_dir / "server.lock", "a+")  # noqa: SIM115 (held for the process lifetime)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        f.seek(0)
        holder = f.read().strip()
        f.close()
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise CacheInUseError(
                f"another canopy server (pid {holder or '?'}) uses the folder {cache_dir}; stop "
                "it or use another cache_dir / state_dir"
            ) from e
        raise LockUnsupportedError(
            f"file locks do not work in {cache_dir} (errno {e.errno}); put state_dir on a local "
            "disk"
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
        lock_cache(cfg.state)
        self.db = JobsDB(cfg.state / DB_FILE, wal=wal)
        self.on_done = on_done
        self.log = get_logger("canopy.jobs")
        self.procs: dict[int, subprocess.Popen[bytes]] = {}
        self._oom_at_start: dict[int, int | None] = {}  # cgroup OOM kills when a worker started
        # memory each running job may use (its admission need): part of the server's account
        self.reserved: dict[int, int] = {}
        # workers of an earlier server process that still run (it crashed or was killed):
        # watched through the database, counted as running
        self.adopted: dict[int, Job] = {}
        self._silent_since: dict[int, float] = {}
        self.cache_bytes: Callable[[], int] = lambda: 0  # set by the app (the dataset cache)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jobs", daemon=True)
        for id in self.db.mark_interrupted():
            self.log.warning("job_interrupted", job=id)
        for j in self.db.find(states=("running",)):
            if j.process_alive():
                self.adopted[j.id] = j
                self.reserved[j.id] = self.need(j)
                self.log.warning("job_adopted", job=j.id, worker_pid=j.pid)
        for id in self.db.requeue_stopped():
            self.log.info("job_requeued", job=id, reason="server_stopped")
        self.clean_tmp()

    def need(self, job: Job) -> int:
        """Memory a job is admitted with: its working memory with margin plus what it reads
        whole (the same estimate the watchdog's limit is built on)."""
        return int(1.5 * self.cfg.jobs.memory_gb * GB) + table_bytes(job, self.cfg.jobs.threads)

    def reserved_bytes(self) -> int:
        with self._lock:
            return sum(self.reserved.values())

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
            if len(self.db.find(states=("queued",))) >= self.cfg.jobs.max_queued:
                raise QueueFullError(self.cfg.jobs.max_queued)
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
        self._watch_adopted()
        self._stop_silent()
        for id in self.db.mark_interrupted():
            self.log.warning("job_interrupted", job=id)
            with self._lock:
                self.reserved.pop(id, None)
        self._start_queued()

    def _watch_adopted(self) -> None:
        for id in list(self.adopted):
            j = self.db.get(id)
            if j is None or j.state != "running" or not j.process_alive():
                self.adopted.pop(id, None)
                with self._lock:
                    self.reserved.pop(id, None)
                self.log.info("job_adopted_ended", job=id, state=j.state if j else None)
                if j is not None and j.state == "done" and self.on_done:
                    self.on_done(j)

    def _stop_silent(self) -> None:
        """A worker that lives but stays silent is asked to stop, then killed after a minute."""
        now = time.time()
        silent = {j.id: j for j in self.db.silent(now=now)}
        for id in list(self._silent_since):
            if id not in silent:
                self._silent_since.pop(id)
        for id, j in silent.items():
            first = self._silent_since.setdefault(id, now)
            sig = signal.SIGTERM if now - first < 60 else signal.SIGKILL
            if j.stop_reason != "no_heartbeat":
                self.db.update(id, stop_reason="no_heartbeat")
                self.log.warning("job_silent_stopping", job=id, worker_pid=j.pid)
            with contextlib.suppress(OSError):
                if j.pid:
                    os.killpg(j.pid, sig)

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
                    # killed by the kernel for lack of memory (the cgroup counted an OOM kill)
                    before = self._oom_at_start.get(id)
                    after = cgroup_memory_limit().get("oom_kills")
                    if code == -9 and before is not None and after is not None and after > before:
                        error = "worker_killed:oom"
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
            with self._lock:
                self.reserved.pop(id, None)
            # the job's config copy holds real paths: not kept once the job ended
            (self.cfg.state / "jobs" / f"job-{id}.json").unlink(missing_ok=True)
            if job.state == "done" and self.on_done:
                self.on_done(job)

    def _start_queued(self) -> None:
        """Start the oldest queued job that fits now. A job that can never fit (more than the
        host or its cgroup has in total, or more than memory.budget_gb) fails; a job waiting for
        memory does not hold back smaller ones behind it. The budget is shared with the dataset
        cache: cache + running jobs' reservations + this job must stay within it."""
        with self._lock:
            running = len(self.procs) + len(self.adopted)
        if running >= self.cfg.jobs.max_running:
            return
        launched = set(self.procs) | set(self.adopted)
        queued = sorted(self.db.find(states=("queued",)), key=lambda j: j.id)
        available = memory_headroom()
        total = memory_total()
        budget = int(self.cfg.memory.budget_gb * GB)
        for job in queued:
            if job.id in launched:
                continue
            need = self.need(job)
            in_use = self.cache_bytes() + self.reserved_bytes()
            info = {
                "need_bytes": need,
                "available_bytes": available,
                "total_bytes": total,
                "budget_bytes": budget,
                "in_use_bytes": in_use,
            }
            if need > total or need > budget:
                self.db.update(
                    job.id,
                    state="failed",
                    error="not_enough_memory",
                    finished=time.time(),
                    info=info,
                )
                self.log.error("job_needs_more_memory_than_allowed", job=job.id, **info)
                continue
            if available < need or in_use + need > budget:
                if job.stage != "waiting_for_memory":
                    self.log.warning("job_waiting_for_memory", job=job.id, **info)
                self.db.update(job.id, stage="waiting_for_memory", info=info)
                continue
            if self._over_store_cap(job):
                continue
            self.clean_tmp()
            with self._lock:
                self.reserved[job.id] = need
            self._launch(job)
            return

    def _over_store_cap(self, job: Job) -> bool:
        """The job would push the converted copies above jobs.stores_max_gb: it fails."""
        cap = self.cfg.jobs.stores_max_gb
        if cap is None:
            return False
        from canopy.convert.anndata import store_disk_estimate

        used = sum(
            _size(d)
            for d in self.cfg.cache_dir.glob("*/c[0-9]*")
            if d.is_dir() and "." not in d.name
        )
        need = store_disk_estimate(job.fingerprint, str(job.options.get("x_source", "X")))
        if used + need <= cap * GB:
            return False
        info = {"stores_bytes": used, "need_bytes": need, "cap_bytes": int(cap * GB)}
        self.db.update(job.id, state="failed", error="stores_full", finished=time.time(), info=info)
        self.log.error("job_stores_full", job=job.id, **info)
        return True

    def _launch(self, job: Job) -> None:
        # The worker gets this server's configuration in a file of its own (written atomically).
        conf = self.cfg.state / "jobs" / f"job-{job.id}.json"
        conf.parent.mkdir(exist_ok=True)
        part = conf.with_suffix(".part")
        part.write_text(self.cfg.model_dump_json())
        part.replace(conf)
        cmd = [sys.executable, "-m", "canopy.jobs.worker", str(job.id), str(conf)]
        threads = str(self.cfg.jobs.threads)
        env = dict(
            os.environ,
            PYTHONUNBUFFERED="1",
            # numeric libraries (MKL, OpenBLAS, numexpr) would otherwise use every core of the host
            OMP_NUM_THREADS=threads,
            MKL_NUM_THREADS=threads,
            OPENBLAS_NUM_THREADS=threads,
            NUMEXPR_NUM_THREADS=threads,
        )
        # The worker's own messages before its logging starts, and fatal errors of libraries
        # (HDF5, compression) land in the log folder, not on the server's terminal.
        log_file = current_log_file()
        log_dir = log_file.parent if log_file else self.cfg.logging.dir
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            err: Any = (log_dir / f"canopy-worker-job{job.id}.stderr.txt").open("ab")
        except OSError:
            err = None
        try:
            proc = subprocess.Popen(  # noqa: S603 (our own interpreter and module, no shell)
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err,
                start_new_session=True,
            )
        finally:
            if err is not None:
                err.close()  # the child keeps its own descriptor
        with self._lock:
            self.procs[job.id] = proc
        self._oom_at_start[job.id] = cgroup_memory_limit().get("oom_kills")
        self.db.update(job.id, stage=None, info=None)
        self.log.info("job_started", job=job.id, worker_pid=proc.pid, kind=job.kind)


def table_bytes(job: Job, threads: int = 4) -> int:
    """Estimated memory the converter reads whole (tables; for SpatialData also points,
    shapes and labels)."""
    from canopy.convert.anndata import job_memory_estimate
    from canopy.inspect.models import Fingerprint

    try:
        fp = Fingerprint.model_validate_json(job.fingerprint)
    except ValueError:
        return 0
    return job_memory_estimate(fp, threads)


def _size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / f).stat().st_size
    return total
