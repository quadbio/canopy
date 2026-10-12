"""A job worker process: `python -m canopy.jobs.worker <job id> <config.json>`.

Claims the job, runs it, records the outcome. Logs to its own file (`canopy-worker-<pid>.jsonl`)
with the same privacy rules as the server, including a progress line every 30 s and memory
samples. Stops cleanly on SIGTERM: the job is then `cancelled`, or `interrupted` when the server
was shutting down (it runs again at the next start); the converter removes its temporary store
and spill files. A watchdog thread writes a heartbeat every 5 s and stops the job if the process
grows beyond its memory limit (`memory_limit`), before the system's out-of-memory killer would
end it without a trace.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from canopy.config import Config, apply_file_access
from canopy.jobs.db import DB_FILE, HEARTBEAT_S, JobsDB
from canopy.logs import MemorySampler, get_logger, setup_logging, timed
from canopy.privacy import installation_salt

GB = 2**30
PROGRESS_LOG_S = 30


class Stop(BaseException):
    """Raised inside the job (at its next progress report) to stop it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_signalled = threading.Event()


def _on_term(*_: Any) -> None:
    _signalled.set()


class Control:
    """Progress reporting, cancellation, heartbeat and the memory watchdog for one job."""

    def __init__(self, db: JobsDB, job: int, limit_bytes: int) -> None:
        self.db, self.job, self.limit = db, job, limit_bytes
        self.log = get_logger("canopy.jobs")
        self.stop_code: str | None = None
        self.peak = 0
        self.stage, self.fraction = "start", 0.0
        self._last = 0.0
        self._db_warned = 0.0
        self._done = threading.Event()
        self._watch = threading.Thread(target=self._watchdog, name="watchdog", daemon=True)
        self._watch.start()

    def progress(self, stage: str, fraction: float) -> None:
        if _signalled.is_set() and not self.stop_code:
            self.stop_code = "signalled"
        if self.stop_code:
            raise Stop(self.stop_code)
        self.stage, self.fraction = stage, fraction
        now = time.monotonic()
        if now - self._last >= 0.5 or fraction >= 1.0:
            self._last = now
            self._write(stage=stage, progress=round(fraction, 4), heartbeat=time.time())

    def finish(self) -> None:
        self._done.set()

    def _write(self, **fields: Any) -> None:
        """Best effort: a busy database (e.g. on network storage) must not fail the job."""
        try:
            self.db.update(self.job, **fields)
        except sqlite3.OperationalError as e:
            if time.monotonic() - self._db_warned > 60:
                self._db_warned = time.monotonic()
                self.log.warning("job_db_busy", job=self.job, error=str(e)[:60])

    def _watchdog(self) -> None:
        proc = psutil.Process()
        last_log = time.monotonic()
        while not self._done.wait(HEARTBEAT_S):
            rss = proc.memory_info().rss
            self.peak = max(self.peak, rss)
            self._write(heartbeat=time.time())
            if time.monotonic() - last_log >= PROGRESS_LOG_S:
                last_log = time.monotonic()
                self.log.info(
                    "job_progress",
                    job=self.job,
                    stage=self.stage,
                    fraction=round(self.fraction, 3),
                    rss_mb=round(rss / 2**20),
                )
            if rss > self.limit and not self.stop_code:
                self.log.error("job_memory_limit", job=self.job, rss=rss, limit=self.limit)
                self.stop_code = "memory_limit"
            if rss > 1.5 * self.limit:  # not reaching a progress report: stop hard
                self._write(state="failed", error="memory_limit", finished=time.time())
                os._exit(3)  # its temporary files are removed by the server


def run(job_id: int, config_file: Path) -> int:
    signal.signal(signal.SIGTERM, _on_term)  # first: a cancel during start-up is not lost
    cfg = Config.model_validate_json(config_file.read_text())
    apply_file_access(cfg)
    setup_logging(cfg.logging, role="worker", salt=installation_salt(cfg.state), to_console=False)
    log = get_logger("canopy.jobs")
    db = JobsDB(cfg.state / DB_FILE, wal=_wal(cfg))
    if not db.claim(job_id, os.getpid()):
        log.warning("job_not_claimed", job=job_id)
        return 0
    job = db.get(job_id)
    assert job is not None
    from canopy.convert.anndata import job_memory_estimate
    from canopy.inspect.models import Fingerprint

    fp = Fingerprint.model_validate_json(job.fingerprint)
    tables = job_memory_estimate(fp, cfg.jobs.threads)
    limit = int((2 * cfg.jobs.memory_gb + 1) * GB + 1.5 * tables)
    sampler = MemorySampler(cfg.logging.memory_sample_interval, cfg.cache_dir)
    sampler.start()
    ctl = Control(db, job_id, limit)
    log.info(
        "job_running",
        job=job_id,
        kind=job.kind,
        dataset_id=job.dataset,
        version=job.version,
        options=job.options,
        memory_gb=cfg.jobs.memory_gb,
        limit_bytes=limit,
        table_bytes_estimate=tables,
    )
    try:
        with timed("job", job=job_id, kind=job.kind, dataset_id=job.dataset) as extra:
            result = _execute(cfg, job.kind, job, fp, ctl)
            extra.update(result)
    except Stop as s:
        code = s.code
        if code == "signalled":
            j = db.get(job_id)
            code = (j.stop_reason if j else None) or "cancelled"
        state = (
            "cancelled"
            if code == "cancelled"
            else "interrupted"
            if code == "server_stopped"
            else "failed"
        )
        _final(db, job_id, state=state, error=code, finished=time.time(), stage=None)
        log.info("job_stopped", job=job_id, reason=code)
        return 0
    except Exception as e:
        code = getattr(e, "code", None) or f"error:{type(e).__name__}"
        _final(db, job_id, state="failed", error=code, finished=time.time())
        log.error("job_failed", job=job_id, code=code, exc_info=True)
        return 1
    finally:
        ctl.finish()
        sampler.stop()
    result["peak_rss_gb"] = round(max(ctl.peak, _max_rss()) / GB, 2)
    _final(db, job_id, state="done", progress=1.0, stage=None, finished=time.time(), result=result)
    log.info("job_done", job=job_id, **result)
    return 0


def _max_rss() -> int:
    """The process's peak resident memory as the system counts it (exact, unlike sampling)."""
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)  # bytes on macOS, KiB on Linux


def _final(db: JobsDB, job: int, **fields: Any) -> None:
    """The outcome must be recorded: retry a busy database for up to a minute."""
    for attempt in range(12):
        try:
            db.update(job, **fields)
            return
        except sqlite3.OperationalError:
            time.sleep(min(10, 2**attempt * 0.25))
    db.update(job, **fields)


def _execute(cfg: Config, kind: str, job: Any, fp: Any, ctl: Control) -> dict[str, Any]:
    if kind != "convert":
        raise ValueError(f"unknown job kind {kind}")
    from canopy.convert.anndata import ConvertOptions, convert_anndata
    from canopy.convert.spatialdata import convert_spatialdata

    opts = ConvertOptions(
        x_source=str(job.options.get("x_source", "X")),
        memory_bytes=int(cfg.jobs.memory_gb * GB),
        tmp_dir=cfg.jobs.tmp_dir,
        job=job.id,
        feature_key=job.options.get("feature_key"),
        threads=cfg.jobs.threads,
    )
    convert = convert_spatialdata if fp.kind == "spatialdata_zarr" else convert_anndata
    r = convert(Path(job.source), fp, cfg.cache_dir, opts, ctl.progress)
    m = r.manifest
    out: dict[str, Any] = {"warnings": sorted(set(r.warnings)), "seconds": m.converter.duration_s}
    tables = list(m.tables.values())
    if len(tables) == 1 and fp.kind != "spatialdata_zarr":
        t = tables[0]
        return out | {"n_obs": t.n_obs, "n_vars": t.n_vars, "nnz": t.nnz, "x_source": t.x_source}
    # SpatialData: sizes of every element (numbers only; names stay out of the job row)
    out["tables"] = [{"n_obs": t.n_obs, "n_vars": t.n_vars, "nnz": t.nnz} for t in tables]
    out["images"] = [e.levels[0] for e in m.images.values()]
    out["labels"] = [{"n_labels": e.n_labels, "shape": e.levels[0]} for e in m.labels.values()]
    out["shapes"] = [{"n": e.n, "kind": e.kind} for e in m.shapes.values()]
    out["points"] = [{"n": e.n, "n_features": e.n_features} for e in m.points.values()]
    return out


def _wal(cfg: Config) -> bool:
    from canopy.catalog.service import use_wal

    return use_wal(cfg)


if __name__ == "__main__":
    sys.exit(run(int(sys.argv[1]), Path(sys.argv[2])))
