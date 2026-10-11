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
    fingerprint TEXT NOT NULL      -- JSON
);
CREATE INDEX IF NOT EXISTS jobs_dataset ON jobs (dataset, id);
"""

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

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Job:
        d = dict(r)
        d["options"] = json.loads(d["options"] or "{}")
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return cls(**d)

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
        if "result" in fields and fields["result"] is not None:
            fields["result"] = json.dumps(fields["result"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as con:
            con.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), id))  # noqa: S608

    def claim(self, id: int, pid: int) -> bool:
        """queued → running for this process; False if the job is no longer queued."""
        with self.connect() as con:
            cur = con.execute(
                "UPDATE jobs SET state='running', pid=?, started=?, heartbeat=? "
                "WHERE id=? AND state='queued'",
                (pid, time.time(), time.time(), id),
            )
            return cur.rowcount == 1

    def mark_interrupted(self) -> list[int]:
        """Running jobs whose process is gone: interrupted (after a crash or restart)."""
        out = []
        for j in self.find(states=("running",)):
            if j.pid is None or not psutil.pid_exists(j.pid):
                self.update(j.id, state="interrupted", error="interrupted", finished=time.time())
                out.append(j.id)
        return out
