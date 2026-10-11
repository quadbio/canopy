"""Job scheduling in the server: queue, start worker processes, watch them, cancel.

A job runs in its own process (`python -m dbx.jobs.worker <job> <config>`), so a conversion that
needs a lot of memory or crashes never takes the server down, and it logs to its own file
(`dbx-worker-<pid>.jsonl`). The scheduler thread starts queued jobs while fewer than
`jobs.max_running` run **and** the host has 1.5 × `jobs.memory_gb` available (the memory guard);
otherwise the job waits, with stage `waiting_for_memory`.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from dbx.config import Config
from dbx.jobs.db import ACTIVE, DB_FILE, Job, JobsDB
from dbx.logs import get_logger

GB = 2**30


class JobManager:
    def __init__(
        self,
        cfg: Config,
        *,
        wal: bool,
        on_done: Callable[[Job], None] | None = None,
        config_file: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = JobsDB(cfg.cache_dir / DB_FILE, wal=wal)
        self.on_done = on_done
        self.log = get_logger("dbx.jobs")
        self.procs: dict[int, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jobs", daemon=True)
        # The worker reads the same configuration as this server.
        self.config_file = config_file or cfg.cache_dir / "jobs-config.json"
        if config_file is None:
            self.config_file.write_text(cfg.model_dump_json())
        for id in self.db.mark_interrupted():
            self.log.warning("job_interrupted", job=id)

    # ------------------------------------------------------------ API
    def submit(
        self, kind: str, dataset: str, version: str, options: dict[str, Any], source: Path, fp: str
    ) -> Job:
        """A new job, or the active one for the same dataset version and options."""
        with self._lock:
            for j in self.db.find(dataset=dataset, states=ACTIVE):
                if j.version == version and j.options == options and j.kind == kind:
                    return j
            id = self.db.add(kind, dataset, version, options, str(source), fp)
        self.log.info("job_queued", job=id, kind=kind, dataset_id=dataset, options=options)
        self._wake.set()
        job = self.db.get(id)
        assert job is not None
        return job

    def cancel(self, id: int) -> Job | None:
        job = self.db.get(id)
        if job is None or job.state not in ACTIVE:
            return job
        with self._lock:
            proc = self.procs.get(id)
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)  # the worker cleans up and records "cancelled"
                self.log.info("job_cancel_requested", job=id)
            elif job.state == "queued":
                self.db.update(id, state="cancelled", finished=time.time(), stage=None)
                self.log.info("job_cancelled", job=id, while_state="queued")
        return self.db.get(id)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        with self._lock:
            procs = list(self.procs.items())
        for id, p in procs:  # a server stop cancels running conversions (their tmp is removed)
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                self.log.info("job_cancel_on_shutdown", job=id)
        for _, p in procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()

    def tick(self) -> None:
        """One scheduling round (also called directly by tests)."""
        self._reap()
        self._start_queued()

    # ------------------------------------------------------------ internals
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
            if job.state in ACTIVE:  # the worker died without recording an outcome
                code = p.returncode
                error = f"worker_killed:{-code}" if code < 0 else f"worker_exit:{code}"
                self.db.update(id, state="failed", error=error, finished=time.time())
                self.log.error("job_worker_died", job=id, returncode=code)
                job = self.db.get(id)
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
        queued = sorted(self.db.find(states=("queued",)), key=lambda j: j.id)
        with self._lock:
            running = len(self.procs)
        for job in queued:
            if job.id in self.procs:
                continue
            if running >= self.cfg.jobs.max_running:
                return
            need = 1.5 * self.cfg.jobs.memory_gb * GB
            available = psutil.virtual_memory().available
            if available < need:
                if job.stage != "waiting_for_memory":
                    self.db.update(job.id, stage="waiting_for_memory")
                    self.log.warning(
                        "job_waiting_for_memory", job=job.id, need=int(need), available=available
                    )
                return
            self._launch(job)
            running += 1

    def _launch(self, job: Job) -> None:
        cmd = [sys.executable, "-m", "dbx.jobs.worker", str(job.id), str(self.config_file)]
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(  # noqa: S603 (our own interpreter and module, no shell)
            cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=None
        )
        with self._lock:
            self.procs[job.id] = proc
        self.log.info("job_started", job=job.id, worker_pid=proc.pid, kind=job.kind)


def options_key(options: dict[str, Any]) -> str:
    return json.dumps(options, sort_keys=True)
