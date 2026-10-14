"""`canopy selfcheck`: is this installation ready, in one command (F13). Each check says ok,
warn or fail with a plain-language line for the owner's terminal (paths and names included:
it is their terminal), and logs only codes and numbers (event `selfcheck`), so the results
reach the next bundle without names.

Checks: the config's paths, the folders Canopy writes (exist or can be made, writable, not open
to other users, file system, free space, file locks), memory (the host's, the account's limit,
the budget and conversions against it), each data root (readable, file system, read speed), the
port, the index and the converted copies.
"""

from __future__ import annotations

import os
import random
import socket
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from canopy.config import Config
from canopy.environment import LOCAL_FS, NETWORK_FS, filesystem
from canopy.logs import cgroup_memory_limit, get_logger

Status = Literal["ok", "warn", "fail"]
DATA_SUFFIXES = (".h5ad", ".zarr")
WALK_LIMIT = 5_000  # entries looked at per root to find a file to time reads on


@dataclass
class Check:
    status: Status
    area: str  # a code: config, folder, memory, data_root, port, index, cache
    message: str  # for the owner's terminal
    facts: dict[str, Any] = field(default_factory=dict)  # codes and numbers only (logged)


def port_free(host: str, port: int) -> int | None:
    """None if a server could listen on host:port, else the errno."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            return e.errno or -1
    return None


def _config_paths(config: Path) -> list[Check]:
    """Relative folders in the config resolve against its own folder: a new release folder
    would start empty (new access link, empty index, every conversion again; host guide §11)."""
    raw = tomllib.loads(config.read_text())
    named = {
        "cache_dir": raw.get("cache_dir"),
        "state_dir": raw.get("state_dir"),
        "logging.dir": (raw.get("logging") or {}).get("dir"),
    }
    relative = [k for k, v in named.items() if isinstance(v, str) and not Path(v).is_absolute()]
    unset = [k for k, v in named.items() if v is None and k != "state_dir"]
    if relative or unset:
        return [
            Check(
                "warn",
                "config",
                f"not absolute in the config: {', '.join(relative + unset)} — a new release "
                "folder would start with an empty cache and index (host guide §11)",
                {"relative": len(relative) + len(unset)},
            )
        ]
    return [Check("ok", "config", "folders in the config are absolute paths")]


def _folder(code: str, path: Path, *, need_locks: bool = False) -> Check:
    facts: dict[str, Any] = {"what": code.replace(" ", "_")}
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".selfcheck-") as f:
            f.write(b"x")
            f.flush()
            if need_locks:
                import fcntl

                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError as e:
                    facts["errno"] = e.errno
                    return Check(
                        "fail",
                        "folder",
                        f"{code} folder {path}: file locks do not work there ({e.strerror}); put "
                        "state_dir on a local disk",
                        facts,
                    )
    except OSError as e:
        facts["errno"] = e.errno
        return Check("fail", "folder", f"{code} folder {path}: cannot write ({e.strerror})", facts)
    fs = filesystem(path)
    facts.update(fstype=fs.get("fstype"), free_gb=fs.get("free_gb"))
    notes = []
    status: Status = "ok"
    if path.stat().st_mode & 0o007:
        status = "warn"
        facts["open_to_others"] = True
        notes.append("readable by other users (chmod o-rwx)")
    if need_locks and fs.get("fstype") not in LOCAL_FS:
        status = "warn"
        notes.append("not on a local disk (the index is safest on one)")
    if code == "cache" and (fs.get("free_gb") or 0) < 50:
        status = "warn"
        notes.append("under 50 GB free for converted copies")
    note = f" — {'; '.join(notes)}" if notes else ""
    return Check(
        status,
        "folder",
        f"{code} folder {path}: writable, {fs.get('fstype')}, {fs.get('free_gb')} GB free{note}",
        facts,
    )


def _memory(cfg: Config) -> list[Check]:
    import psutil

    total_gb = psutil.virtual_memory().total / 2**30
    cg = cgroup_memory_limit()
    limit = cg.get("limit_bytes")
    usable = min(total_gb, limit / 2**30) if limit else total_gb
    facts = {
        "host_total_gb": round(total_gb, 1),
        "limit_gb": round(limit / 2**30, 1) if limit else None,
        "budget_gb": cfg.memory.budget_gb,
        "max_running": cfg.jobs.max_running,
        "job_memory_gb": cfg.jobs.memory_gb,
    }
    where = (
        f"the account's limit {limit / 2**30:.0f} GB"
        if limit
        else f"the host's {total_gb:.0f} GB (no account limit)"
    )
    # conversion workers may grow 0.5 × memory_gb + 1 GB past their reservation (app.py)
    need = cfg.memory.budget_gb + cfg.jobs.max_running * (0.5 * cfg.jobs.memory_gb + 1)
    if need > usable * 0.9:
        return [
            Check(
                "fail",
                "memory",
                f"budget {cfg.memory.budget_gb:g} GB + {cfg.jobs.max_running} conversions × "
                f"{cfg.jobs.memory_gb:g} GB can exceed {where}: lower memory.budget_gb or "
                "jobs.max_running (host guide §5)",
                facts,
            )
        ]
    return [
        Check(
            "ok",
            "memory",
            f"budget {cfg.memory.budget_gb:g} GB and {cfg.jobs.max_running} conversions of "
            f"{cfg.jobs.memory_gb:g} GB fit in {where}",
            facts,
        )
    ]


def _sample_file(root: Path) -> Path | None:
    """The largest regular file among the first WALK_LIMIT entries below `root`."""
    best: tuple[int, Path] | None = None
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in filenames:
            seen += 1
            p = Path(dirpath) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if best is None or size > best[0]:
                best = (size, p)
        if seen >= WALK_LIMIT:
            break
    return best[1] if best else None


def _read_speed(path: Path, read_mb: int) -> dict[str, float]:
    """Sequential MB/s over up to `read_mb` MB, and the median of 32 random 64 KiB reads."""
    size = path.stat().st_size
    fd = os.open(path, os.O_RDONLY)
    try:
        want = min(size, read_mb * 2**20)
        t0 = time.perf_counter()
        done = 0
        while done < want:
            chunk = os.read(fd, min(8 * 2**20, want - done))
            if not chunk:
                break
            done += len(chunk)
        seq_s = time.perf_counter() - t0
        lat = []
        if size > 2**20:
            rng = random.Random(0)  # noqa: S311 - offsets for a timing test
            for _ in range(32):
                off = rng.randrange(0, size - 65536)
                t = time.perf_counter()
                os.pread(fd, 65536, off)
                lat.append((time.perf_counter() - t) * 1000)
    finally:
        os.close(fd)
    out = {"read_mb": round(done / 2**20, 1), "mb_per_s": round(done / 2**20 / max(seq_s, 1e-6), 1)}
    if lat:
        out["random_64k_ms"] = round(sorted(lat)[len(lat) // 2], 2)
    return out


def _roots(cfg: Config, read_mb: int) -> list[Check]:
    out = []
    for i, r in enumerate(cfg.data_roots):
        facts: dict[str, Any] = {"root": i}
        try:
            n = len(os.listdir(r.path))
        except OSError as e:
            out.append(
                Check(
                    "fail",
                    "data_root",
                    f"data root {r.name!r} ({r.path}): cannot list ({e.strerror})",
                    {**facts, "errno": e.errno},
                )
            )
            continue
        fs = filesystem(r.path)
        facts.update(entries=n, fstype=fs.get("fstype"))
        line = f"data root {r.name!r}: {n} entries, {fs.get('fstype')}"
        status: Status = "ok"
        sample = _sample_file(r.path) if read_mb > 0 else None
        if sample is not None:
            try:
                speed = _read_speed(sample, read_mb)
                facts.update(speed)
                line += f"; read {speed['read_mb']:g} MB at {speed['mb_per_s']:g} MB/s"
                if "random_64k_ms" in speed:
                    line += f", random 64 KB reads {speed['random_64k_ms']:g} ms"
                if speed["read_mb"] >= 64 and speed["mb_per_s"] < 100:
                    status = "warn"
                    line += " — slow: large datasets will open slowly (convert them)"
            except OSError as e:
                status = "warn"
                facts["read_errno"] = e.errno
                line += f"; a test read failed ({e.strerror})"
        if fs.get("fstype") in NETWORK_FS:
            line += (
                " (network file system: speeds vary with load; a repeated run may read from memory)"
            )
        out.append(Check(status, "data_root", line, facts))
    return out


def _port(cfg: Config) -> Check:
    err = port_free(cfg.server.host, cfg.server.port)
    if err is None:
        return Check(
            "ok",
            "port",
            f"port {cfg.server.port} on {cfg.server.host} is free",
            {"port_free": True},
        )
    return Check(
        "warn",
        "port",
        f"port {cfg.server.port} is in use (errno {err}): a running Canopy server, or choose "
        "server.port",
        {"port_free": False, "errno": err},
    )


def _index_and_cache(cfg: Config) -> list[Check]:
    from canopy.catalog.db import DB_FILE, CatalogDB

    db_path = cfg.state / DB_FILE
    if not db_path.exists():
        return [
            Check(
                "warn",
                "index",
                "no index yet: it is made when the server first starts",
                {"datasets": 0},
            )
        ]
    db = CatalogDB(db_path, wal=filesystem(cfg.state).get("fstype") in LOCAL_FS)
    users = db.version_users()
    last = db.last_scan()
    with db.connect() as con:
        states = dict(con.execute("SELECT state, COUNT(*) FROM datasets GROUP BY state").fetchall())
    n = sum(states.values())
    status: Status = "ok"
    line = f"index: {n} datasets {states}"
    if last:
        line += f"; last scan {last['status']}"
        if last["status"] == "failed":
            status = "warn"
    checks = [
        Check(
            status, "index", line, {"datasets": n, **{f"state_{k}": v for k, v in states.items()}}
        )
    ]
    stores = (
        [d for d in cfg.cache_dir.iterdir() if d.is_dir() and any(d.glob("c*/manifest.json"))]
        if cfg.cache_dir.is_dir()
        else []
    )
    unused = [d for d in stores if d.name not in users]
    unused_gb = sum(f.stat().st_size for d in unused for f in d.rglob("*") if f.is_file()) / 2**30
    checks.append(
        Check(
            "warn" if unused_gb >= 10 else "ok",
            "cache",
            f"{len(stores)} converted copies, {len(unused)} unused ({unused_gb:.2f} GB)"
            + (" — `canopy cache prune --config …` frees them (host guide §13)" if unused else ""),
            {"stores": len(stores), "unused": len(unused), "unused_gb": round(unused_gb, 2)},
        )
    )
    return checks


def run(cfg: Config, config: Path, read_mb: int = 256) -> list[Check]:
    """Every check, in order; one that raises is reported as failed (never stops the rest)."""
    steps: list[Callable[[], list[Check]]] = [
        lambda: _config_paths(config),
        lambda: [_folder("cache", cfg.cache_dir)],
        lambda: [_folder("state", cfg.state, need_locks=True)],
        lambda: [_folder("log", cfg.logging.dir)],
        lambda: [_folder("job tmp", cfg.jobs.tmp_dir)] if cfg.jobs.tmp_dir else [],
        lambda: _memory(cfg),
        lambda: _roots(cfg, read_mb),
        lambda: [_port(cfg)],
        lambda: _index_and_cache(cfg),
    ]
    out: list[Check] = []
    log = get_logger("canopy.selfcheck")
    for step in steps:
        try:
            out += step()
        except Exception as e:  # a broken check is a finding, not a crash
            out.append(
                Check(
                    "fail",
                    "selfcheck",
                    f"a check failed: {type(e).__name__}: {e}",
                    {"error": type(e).__name__},
                )
            )
    for c in out:
        log.info("selfcheck", area=c.area, status=c.status, **c.facts)
    return out
