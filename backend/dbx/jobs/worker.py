"""A job worker process: `python -m dbx.jobs.worker <job id> <config.json>`.

Claims the job, runs it, records the outcome. Logs to its own file (`dbx-worker-<pid>.jsonl`)
with the same privacy rules as the server. Stops cleanly on SIGTERM (cancel): the converter
removes its temporary store and files. A watchdog thread stops the job if the process grows
beyond twice `jobs.memory_gb` plus 1 GB (`memory_limit`), before the system's out-of-memory
killer would end it without a trace.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from dbx.config import Config
from dbx.jobs.db import DB_FILE, JobsDB
from dbx.logs import get_logger, setup_logging, timed
from dbx.privacy import installation_salt

GB = 2**30


class Stop(BaseException):
    """Raised inside the job (at its next progress report) to stop it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Control:
    """Progress reporting, cancellation and the memory watchdog for one job."""

    def __init__(self, db: JobsDB, job: int, limit_bytes: int) -> None:
        self.db, self.job, self.limit = db, job, limit_bytes
        self.stop_code: str | None = None
        self.peak = 0
        self._last = 0.0
        self._done = threading.Event()
        self._watch = threading.Thread(target=self._watchdog, name="watchdog", daemon=True)
        self._watch.start()

    def progress(self, stage: str, fraction: float) -> None:
        if self.stop_code:
            raise Stop(self.stop_code)
        now = time.monotonic()
        if now - self._last >= 0.5 or fraction >= 1.0:
            self._last = now
            self.db.update(
                self.job, stage=stage, progress=round(fraction, 4), heartbeat=time.time()
            )

    def cancel(self, *_: Any) -> None:
        self.stop_code = self.stop_code or "cancelled"

    def finish(self) -> None:
        self._done.set()

    def _watchdog(self) -> None:
        proc = psutil.Process()
        while not self._done.wait(1.0):
            rss = proc.memory_info().rss
            self.peak = max(self.peak, rss)
            if rss > self.limit and not self.stop_code:
                get_logger("dbx.jobs").error(
                    "job_memory_limit", job=self.job, rss=rss, limit=self.limit
                )
                self.stop_code = "memory_limit"
            if rss > 1.5 * self.limit:  # not reaching a progress report: stop hard
                self.db.update(self.job, state="failed", error="memory_limit", finished=time.time())
                os._exit(3)


def run(job_id: int, config_file: Path) -> int:
    cfg = Config.model_validate_json(config_file.read_text())
    setup_logging(
        cfg.logging, role="worker", salt=installation_salt(cfg.cache_dir), to_console=False
    )
    log = get_logger("dbx.jobs")
    db = JobsDB(cfg.cache_dir / DB_FILE, wal=_wal(cfg))
    if not db.claim(job_id, os.getpid()):
        log.warning("job_not_claimed", job=job_id)
        return 0
    job = db.get(job_id)
    assert job is not None
    ctl = Control(db, job_id, int((2 * cfg.jobs.memory_gb + 1) * GB))
    signal.signal(signal.SIGTERM, ctl.cancel)
    log.info("job_running", job=job_id, kind=job.kind, dataset_id=job.dataset, options=job.options)
    try:
        with timed("job", job=job_id, kind=job.kind, dataset_id=job.dataset) as extra:
            result = _execute(cfg, job.kind, job, ctl)
            extra.update(result)
    except Stop as s:
        state = "cancelled" if s.code == "cancelled" else "failed"
        db.update(job_id, state=state, error=s.code, finished=time.time(), stage=None)
        log.info("job_stopped", job=job_id, reason=s.code)
        return 0
    except Exception as e:
        code = getattr(e, "code", None) or f"error:{type(e).__name__}"
        db.update(job_id, state="failed", error=code, finished=time.time())
        log.error("job_failed", job=job_id, code=code, exc_info=True)
        return 1
    finally:
        ctl.finish()
    result["peak_rss_gb"] = round(ctl.peak / GB, 2)
    db.update(job_id, state="done", progress=1.0, stage=None, finished=time.time(), result=result)
    log.info("job_done", job=job_id, **result)
    return 0


def _execute(cfg: Config, kind: str, job: Any, ctl: Control) -> dict[str, Any]:
    if kind != "convert":
        raise ValueError(f"unknown job kind {kind}")
    from dbx.convert.anndata import ConvertOptions, convert_anndata
    from dbx.inspect.models import Fingerprint

    fp = Fingerprint.model_validate_json(job.fingerprint)
    opts = ConvertOptions(
        x_source=str(job.options.get("x_source", "X")),
        memory_bytes=int(cfg.jobs.memory_gb * GB),
        tmp_dir=cfg.jobs.tmp_dir,
    )
    r = convert_anndata(Path(job.source), fp, cfg.cache_dir, opts, ctl.progress)
    t = next(iter(r.manifest.tables.values()))
    return {
        "warnings": r.warnings,
        "n_obs": t.n_obs,
        "n_vars": t.n_vars,
        "nnz": t.nnz,
        "x_source": t.x_source,
        "seconds": r.manifest.converter.duration_s,
    }


def _wal(cfg: Config) -> bool:
    from dbx.catalog.service import use_wal

    return use_wal(cfg)


if __name__ == "__main__":
    sys.exit(run(int(sys.argv[1]), Path(sys.argv[2])))
