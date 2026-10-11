"""The catalog index: one SQLite file in the cache folder (rebuildable at any time).

A connection is opened per operation, so the scanner thread and API requests never share one.
WAL lets readers proceed during a scan; on network file systems (no shared memory for WAL) the
classic rollback journal is used instead.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

SCHEMA_VERSION = "2"
DB_FILE = "catalog.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,         -- stable per (root, relpath), across versions of the file
    root TEXT NOT NULL,
    relpath TEXT NOT NULL,
    folder TEXT NOT NULL,        -- parent relpath, '' at the root
    name TEXT NOT NULL,
    kind TEXT,                   -- h5ad | anndata_zarr | spatialdata_zarr
    stamp TEXT NOT NULL,         -- version stamp at the last inspection
    version TEXT,                -- fingerprint dataset_id
    size_bytes INTEGER,
    mtime REAL,
    state TEXT NOT NULL,
    reason TEXT,
    summary TEXT,                -- JSON, for listings
    fingerprint TEXT,            -- JSON, standard level
    inspect_ms REAL,
    first_seen REAL NOT NULL,
    updated REAL NOT NULL,
    last_scan INTEGER NOT NULL,
    UNIQUE (root, relpath)
);
CREATE INDEX IF NOT EXISTS ix_datasets_folder ON datasets (root, folder);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    full INTEGER NOT NULL,
    started REAL NOT NULL,
    finished REAL,
    status TEXT NOT NULL,        -- running | done | failed
    pid INTEGER,                 -- process running the scan (server or `dbx catalog scan`)
    counts TEXT,                 -- JSON
    error TEXT
);
"""

ROW_FIELDS = (
    "id",
    "root",
    "relpath",
    "folder",
    "name",
    "kind",
    "stamp",
    "version",
    "size_bytes",
    "mtime",
    "state",
    "reason",
    "summary",
    "fingerprint",
    "inspect_ms",
)


# SQL assembled once from the constant column names above (never from input).
_LIST_COLS = ", ".join(k for k in ROW_FIELDS if k != "fingerprint")
_UPSERT = (
    f"INSERT INTO datasets ({', '.join(ROW_FIELDS)}, first_seen, updated, last_scan) "  # noqa: S608
    f"VALUES ({', '.join(f':{k}' for k in ROW_FIELDS)}, :now, :now, :scan) "
    "ON CONFLICT(root, relpath) DO UPDATE SET "
    + ", ".join(f"{k}=excluded.{k}" for k in ROW_FIELDS if k != "id")
    + ", updated=excluded.updated, last_scan=excluded.last_scan"
)
_FOLDER = f"SELECT {_LIST_COLS} FROM datasets WHERE root=? AND folder=? ORDER BY name"  # noqa: S608
_SEARCH = f"SELECT {_LIST_COLS} FROM datasets WHERE relpath LIKE ? ESCAPE '\\'"  # noqa: S608


@dataclass
class Row:
    id: str
    root: str
    relpath: str
    folder: str
    name: str
    kind: str | None
    stamp: str
    version: str | None
    size_bytes: int | None
    mtime: float | None
    state: str
    reason: str | None
    summary: dict[str, Any] = field(default_factory=dict)
    fingerprint: str | None = None
    inspect_ms: float | None = None


def _row(r: sqlite3.Row) -> Row:
    d = {k: r[k] for k in ROW_FIELDS if k in r.keys()}  # noqa: SIM118 - sqlite3.Row
    d["summary"] = json.loads(d["summary"]) if d.get("summary") else {}
    return Row(**d)


def _like_prefix(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CatalogDB:
    def __init__(self, path: Path, *, wal: bool = True) -> None:
        self.path = path
        self.wal = wal
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(_SCHEMA)
            v = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if v is None:
                con.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
            elif v[0] != SCHEMA_VERSION:  # the index is rebuildable: start over
                con.executescript("DROP TABLE datasets; DROP TABLE scans;" + _SCHEMA)
                con.execute("UPDATE meta SET value=? WHERE key='schema'", (SCHEMA_VERSION,))
            # Scans whose process is gone were interrupted (crash or restart). A scan running in
            # another live process (the server, while `dbx catalog scan` runs) is left alone.
            running = con.execute("SELECT id, pid FROM scans WHERE status='running'").fetchall()
            for r in running:
                if r["pid"] is None or not psutil.pid_exists(r["pid"]):
                    con.execute(
                        "UPDATE scans SET status='failed', error='interrupted' WHERE id=?",
                        (r["id"],),
                    )

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

    def versions(self) -> set[str]:
        """Every dataset version the index knows (converted stores are kept for these)."""
        with self.connect() as con:
            return {
                r[0] for r in con.execute("SELECT version FROM datasets WHERE version IS NOT NULL")
            }

    # ------------------------------------------------------------ scans
    def start_scan(self, trigger: str, full: bool) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO scans (trigger, full, started, status, pid) "
                "VALUES (?, ?, ?, 'running', ?)",
                (trigger, int(full), time.time(), os.getpid()),
            )
            return int(cur.lastrowid or 0)

    def finish_scan(self, scan: int, counts: dict[str, Any], error: str | None = None) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE scans SET finished=?, status=?, counts=?, error=? WHERE id=?",
                (time.time(), "failed" if error else "done", json.dumps(counts), error, scan),
            )

    def last_scan(self) -> dict[str, Any] | None:
        with self.connect() as con:
            r = con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        if r is None:
            return None
        d = dict(r)
        d["full"] = bool(d["full"])
        d["counts"] = json.loads(d["counts"]) if d["counts"] else None
        return d

    # ------------------------------------------------------------ datasets
    def stamps(self, root: str) -> dict[str, tuple[str, str]]:
        """relpath -> (stamp, state) of a root's indexed datasets."""
        with self.connect() as con:
            rows = con.execute("SELECT relpath, stamp, state FROM datasets WHERE root=?", (root,))
            return {r["relpath"]: (r["stamp"], r["state"]) for r in rows}

    def upsert(self, rows: Iterable[dict[str, Any]], scan: int) -> None:
        now = time.time()
        with self.connect() as con:
            for r in rows:
                vals = {k: r.get(k) for k in ROW_FIELDS}
                vals["summary"] = json.dumps(r.get("summary") or {})
                con.execute(_UPSERT, {**vals, "now": now, "scan": scan})

    def touch(self, root: str, relpaths: Iterable[str], scan: int) -> None:
        with self.connect() as con:
            con.executemany(
                "UPDATE datasets SET last_scan=? WHERE root=? AND relpath=?",
                [(scan, root, p) for p in relpaths],
            )

    def remove_unseen(self, root: str, scan: int) -> int:
        with self.connect() as con:
            cur = con.execute("DELETE FROM datasets WHERE root=? AND last_scan<>?", (root, scan))
            return cur.rowcount

    def remove_roots_except(self, roots: Iterable[str]) -> int:
        keep = set(roots)
        with self.connect() as con:
            stale = [
                r[0] for r in con.execute("SELECT DISTINCT root FROM datasets") if r[0] not in keep
            ]
            return sum(
                con.execute("DELETE FROM datasets WHERE root=?", (r,)).rowcount for r in stale
            )

    def get(self, id: str) -> Row | None:
        with self.connect() as con:
            r = con.execute("SELECT * FROM datasets WHERE id=?", (id,)).fetchone()
        return _row(r) if r else None

    def root_counts(self) -> dict[str, dict[str, int]]:
        with self.connect() as con:
            rows = con.execute("SELECT root, state, COUNT(*) AS n FROM datasets GROUP BY 1, 2")
            out: dict[str, dict[str, int]] = {}
            for r in rows:
                out.setdefault(r["root"], {})[r["state"]] = r["n"]
        return out

    def folder(self, root: str, folder: str) -> tuple[list[Row], dict[str, int]]:
        """Datasets directly in `folder`, and the subfolders below it that contain datasets
        (name -> number of datasets anywhere below)."""
        with self.connect() as con:
            here = con.execute(_FOLDER, (root, folder)).fetchall()
            if folder:
                below = con.execute(
                    "SELECT folder FROM datasets WHERE root=? AND folder LIKE ? ESCAPE '\\'",
                    (root, _like_prefix(folder + "/") + "%"),
                ).fetchall()
            else:
                below = con.execute(
                    "SELECT folder FROM datasets WHERE root=? AND folder<>''", (root,)
                ).fetchall()
        start = len(folder) + 1 if folder else 0
        sub: dict[str, int] = {}
        for r in below:
            name = r["folder"][start:].split("/", 1)[0]
            sub[name] = sub.get(name, 0) + 1
        return [_row(r) for r in here], dict(sorted(sub.items()))

    def search(self, q: str, *, root: str | None, limit: int) -> list[Row]:
        sql = _SEARCH
        args: list[Any] = ["%" + _like_prefix(q) + "%"]
        if root is not None:
            sql += " AND root=?"
            args.append(root)
        sql += " ORDER BY (name LIKE ? ESCAPE '\\') DESC, root, relpath LIMIT ?"
        args += ["%" + _like_prefix(q) + "%", limit]
        with self.connect() as con:
            return [_row(r) for r in con.execute(sql, args)]
