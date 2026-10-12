"""The job table: one SQLite file in the cache folder, shared by the server and its workers.

States: queued → running → done | failed | cancelled; a job whose process vanished (crash,
restart, killed by the system) becomes `interrupted`. Workers write progress at most twice a
second; the server reads it for the API.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

DB_FILE = "jobs.sqlite"
ACTIVE = ("queued", "running")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- convert
    dataset TEXT NOT NULL,         -- catalog id
    version TEXT NOT NULL,         -- dataset version (fingerprint id) converted
    options TEXT NOT NULL,         -- JSON
    state TEXT NOT NULL,
    stage TEXT,
    progress REAL NOT NULL DEFAULT 0,
    pid INTEGER,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    heartbeat REAL,
    error TEXT,                    -- short code
    result TEXT,                   -- JSON
    source TEXT NOT NULL,          -- real path of the source (resolved by the server)
    fingerprint TEXT NOT NULL,     -- JSON
    pid_start REAL,                -- start time of the worker process (rules out a reused pid)
    stop_reason TEXT,              -- why the server asked the worker to stop
    info TEXT                      -- JSON shown with the job (e.g. memory numbers while waiting)
);
CREATE INDEX IF NOT EXISTS jobs_dataset ON jobs (dataset, id);
"""
# Columns added after the first release of the table (added to old databases on open).
_ADDED = {"pid_start": "REAL", "stop_reason": "TEXT", "info": "TEXT"}
HEARTBEAT_S = 5  # the worker writes a heartbeat this often
STALE_S = 120  # a running job without a heartbeat for this long is checked

PUBLIC = (
    "id",
    "kind",
    "dataset",
    "version",
    "options",
    "state",
    "stage",
    "progress",
    "created",
    "started",
    "finished",
    "error",
    "result",
    "info",
)


@dataclass
class Job:
    id: int
    kind: str
    dataset: str
    version: str
    options: dict[str, Any]
    state: str
    stage: str | None
    progress: float
    pid: int | None
    created: float
    started: float | None
    finished: float | None
    heartbeat: float | None
    error: str | None
    result: dict[str, Any] | None
    source: str
    fingerprint: str
    pid_start: float | None = None
    stop_reason: str | None = None
    info: dict[str, Any] | None = None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Job:
        d = dict(r)
        d["options"] = json.loads(d["options"] or "{}")
        d["result"] = json.loads(d["result"]) if d["result"] else None
        d["info"] = json.loads(d["info"]) if d.get("info") else None
        return cls(**d)

    def process_alive(self) -> bool:
        """The job's worker process still runs (same pid *and* same start time)."""
        if self.pid is None:
            return False
        try:
            p = psutil.Process(self.pid)
            return self.pid_start is None or abs(p.create_time() - self.pid_start) < 1.0
        except psutil.Error:
            return False

    def public(self) -> dict[str, Any]:
        """What the API shows (no paths)."""
        return {k: getattr(self, k) for k in PUBLIC}


class JobsDB:
    def __init__(self, path: Path, *, wal: bool = True) -> None:
        self.path = path
        self.wal = wal
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(_SCHEMA)
            have = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
            for col, typ in _ADDED.items():
                if col not in have:
                    con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute(f"PRAGMA journal_mode={'WAL' if self.wal else 'DELETE'}")
            con.execute("PRAGMA synchronous=NORMAL")
            with con:
                yield con
        finally:
            con.close()

    def add(
        self,
        kind: str,
        dataset: str,
        version: str,
        options: dict[str, Any],
        source: str,
        fingerprint: str,
    ) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO jobs (kind, dataset, version, options, state, created, source, "
                "fingerprint) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                (kind, dataset, version, json.dumps(options), time.time(), source, fingerprint),
            )
            return int(cur.lastrowid or 0)

    def get(self, id: int) -> Job | None:
        with self.connect() as con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
        return Job.from_row(r) if r else None

    def find(self, *, dataset: str | None = None, states: tuple[str, ...] = ()) -> list[Job]:
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        if dataset is not None:
            q += " AND dataset=?"
            args.append(dataset)
        if states:
            q += f" AND state IN ({','.join('?' * len(states))})"
            args += list(states)
        with self.connect() as con:
            rows = con.execute(q + " ORDER BY id DESC LIMIT 200", args).fetchall()
        return [Job.from_row(r) for r in rows]

    def update(self, id: int, **fields: Any) -> None:
        for k in ("result", "info"):
            if k in fields and fields[k] is not None:
                fields[k] = json.dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as con:
            con.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), id))  # noqa: S608

    def claim(self, id: int, pid: int) -> bool:
        """queued → running for this process; False if the job is no longer queued."""
        start = psutil.Process(pid).create_time()
        with self.connect() as con:
            cur = con.execute(
                "UPDATE jobs SET state='running', pid=?, pid_start=?, started=?, heartbeat=?, "
                "stop_reason=NULL, info=NULL WHERE id=? AND state='queued'",
                (pid, start, time.time(), time.time(), id),
            )
            return cur.rowcount == 1

    def mark_interrupted(self, *, now: float | None = None) -> list[int]:
        """Running jobs whose worker is gone (crash, kill, restart, reused pid): `interrupted`
        (`no_heartbeat` when the server had stopped it for being silent)."""
        now = now or time.time()
        out = []
        for j in self.find(states=("running",)):
            if not j.process_alive():
                reason = "no_heartbeat" if j.stop_reason == "no_heartbeat" else "interrupted"
                self.update(j.id, state="interrupted", error=reason, finished=now)
                out.append(j.id)
        return out

    def silent(self, *, now: float | None = None) -> list[Job]:
        """Running jobs whose process lives but sent no heartbeat for STALE_S (hung, e.g. on a
        stuck network read): the manager stops them; they stay `running` until they exit, so
        their temporary files are not removed under them."""
        now = now or time.time()
        return [
            j
            for j in self.find(states=("running",))
            if j.process_alive() and (now - (j.heartbeat or j.started or j.created)) > STALE_S
        ]

    def requeue_stopped(self) -> list[int]:
        """Jobs stopped by a server shutdown run again at the next start (plan F8)."""
        out = []
        for j in self.find(states=("interrupted",)):
            if j.error == "server_stopped":
                self.update(
                    j.id, state="queued", error=None, finished=None, progress=0.0, stage=None
                )
                out.append(j.id)
        return out
