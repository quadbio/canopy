"""`canopy bundle`: everything the developer needs from a host, in one archive, at `strict`.

What goes in (and only this):
- the log lines of every Canopy process of the last days (server, workers, CLI, start-up
  failures), each line re-filtered at strict privacy whatever level the run used: name fields
  become salted tokens, free text (exception and warning messages, browser texts) is dropped;
- crash and hang stack files and worker error output, reduced to code locations;
- the environment (versions, CPU and memory, file system types, cgroup memory limits);
- the job table and the index as summaries: states, reason codes, sizes, timings; ids as tokens.

Before writing, the archive is checked for every name known on this host (data roots and their
paths, every file, folder, column and element name in the index, the user and host names) and
refused if one is found. A `MANIFEST.md` inside lists what is included. Open the archive and read
it before copying it anywhere.
"""

from __future__ import annotations

import contextlib
import getpass
import io
import json
import os
import re
import socket
import sqlite3
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from canopy.config import Config
from canopy.logs import NAME_FIELDS, default_log_dir
from canopy.privacy import code_location, installation_salt, token
from canopy.telemetry import PLAIN_CODE

# Fields holding free text in log lines: dropped at strict.
TEXT_FIELDS = frozenset({"exc_message", "exception", "message", "c_message", "c_code", "c_kind"})
# Lines of stack files and worker error output that are code locations or fault headers.
_STACK_LINE = re.compile(
    r'^\s*(File "(?P<file>[^"]+)", line (?P<line>\d+)(?: in (?P<fn>\S+))?|Fatal Python error|'
    r"Current thread|Thread 0x|Stack \(most recent|Extension modules|Segmentation fault|Bus error|"
    r"Traceback \(most recent call last\)|\w+Error:?|\w+Exception:?)"
)


def _strictify(value: Any, salt: str, key: str = "") -> Any:
    """A log value at strict: name fields tokenised (nested), free text dropped."""
    if key in ("c_code", "c_kind") and isinstance(value, str) and PLAIN_CODE.fullmatch(value):
        return value  # as the server keeps them at strict (telemetry.py)
    if key in TEXT_FIELDS:
        return "(dropped)"
    if key in NAME_FIELDS:
        return _tokens(value, salt)
    if isinstance(value, dict):
        return {k: _strictify(v, salt, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_strictify(v, salt) for v in value]
    return value


def _tokens(value: Any, salt: str) -> Any:
    if isinstance(value, str):
        return value if re.fullmatch(r"h_[0-9a-f]{10}", value) else token(value, salt)
    if isinstance(value, list):
        return [_tokens(v, salt) for v in value]
    if isinstance(value, dict):
        return {k: _tokens(v, salt) for k, v in value.items()}
    return value


_CODE = re.compile(r"[a-z0-9_.:]+")


def _strict_event(event: dict[str, Any], salt: str) -> dict[str, Any]:
    """A log line at strict. A third-party library's line (logger outside canopy) carries its
    message as `event`, kept (partly redacted) at standard and raw at debug: dropped here
    unless it is a plain code."""
    out = _strictify(event, salt)
    if isinstance(out, dict) and not str(out.get("logger", "canopy")).startswith("canopy"):
        text = out.get("event")
        if isinstance(text, str) and not _CODE.fullmatch(text):
            out["event"] = "dropped_library_text"
    return out  # type: ignore[no-any-return]


def _log_lines(path: Path, salt: str) -> tuple[str, int]:
    out, dropped = [], 0
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            dropped += 1  # not a JSON line (should not happen): left out
            continue
        out.append(json.dumps(_strict_event(event, salt), separators=(",", ":")))
    return "\n".join(out) + ("\n" if out else ""), dropped


def _code_only(text: str) -> str:
    """Stack files and error output reduced to code locations and fault headers."""
    out = []
    for line in text.splitlines():
        m = _STACK_LINE.match(line)
        if not m:
            continue
        if m.group("file"):
            out.append(
                f"  {code_location(m.group('file'))}:{m.group('line')} {m.group('fn') or ''}"
            )
        else:
            out.append(re.split(r":\s", line.strip(), maxsplit=1)[0])
    return "\n".join(out) + ("\n" if out else "")


def _sql(db: Path, query: str) -> list[dict[str, Any]]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(query)]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _jobs(cfg: Config, salt: str) -> list[dict[str, Any]]:
    rows = _sql(
        cfg.state / "jobs.sqlite",
        "SELECT id, kind, dataset, version, state, stage, progress, created, started, finished, "
        "error, result, info, stop_reason FROM jobs ORDER BY id",
    )
    for r in rows:
        r["dataset"] = token(str(r["dataset"]), salt)
        for k in ("result", "info"):
            try:
                r[k] = _strictify(json.loads(r[k]), salt) if r[k] else None
            except ValueError:
                r[k] = None
    return rows


def _catalog(cfg: Config, salt: str) -> list[dict[str, Any]]:
    rows = _sql(
        cfg.state / "catalog.sqlite",
        "SELECT id, root, kind, state, reason, size_bytes, inspect_ms, summary FROM datasets",
    )
    out = []
    for r in rows:
        try:
            summary = json.loads(r["summary"] or "{}")
        except ValueError:
            summary = {}
        out.append(
            {
                "dataset_id": token(str(r["id"]), salt),
                "root": token(str(r["root"]), salt),
                "kind": r["kind"],
                "state": r["state"],
                "reason": r["reason"],
                "size_bytes": r["size_bytes"],
                "inspect_ms": r["inspect_ms"],
                **{
                    k: summary.get(k)
                    for k in ("n_obs", "n_vars", "x_encoding", "n_obs_columns", "n_tables")
                    if k in summary
                },
                "n_embeddings": len(summary.get("embeddings") or []),
                "warnings": summary.get("warnings", []),
            }
        )
    return out


def _host_facts(cfg: Config, salt: str) -> dict[str, Any]:
    from canopy.environment import environment_snapshot
    from canopy.logs import memory_snapshot

    facts: dict[str, Any] = {
        "environment": _strictify(environment_snapshot(cfg), salt),
        "memory": memory_snapshot(cfg.cache_dir),
        "privacy_of_runs": "re-filtered to strict by canopy bundle",
    }
    cgroup: dict[str, Any] = {}
    with contextlib.suppress(OSError):
        cgroup["proc_self_cgroup"] = Path("/proc/self/cgroup").read_text().splitlines()
    from canopy.logs import cgroup_memory_limit

    cgroup["limit"] = cgroup_memory_limit()
    facts["cgroup"] = cgroup
    return facts


# Fingerprint maps whose keys are user-chosen names (the other keys are the format's own).
_NAMED_KEYS = frozenset(
    {
        "layers",
        "obsm",
        "varm",
        "obsp",
        "varp",
        "uns",
        "images",
        "labels",
        "points",
        "shapes",
        "tables",
        "table_refs",
        "columns",
    }
)
_HASH = re.compile(r"^[0-9a-f]{12,}$")
# Version numbers (a store's `software_version`, package versions): not names, and they match the
# installed versions in every start-up line.
_VERSION = re.compile(r"v?\d+(\.\d+)+([.+-]?[A-Za-z0-9]+)*")
# Fingerprint fields that hold format facts, not names (their values are not searched for).
_FACT_KEYS = frozenset(
    {"kind", "encoding", "dtype", "layout", "dataset_id", "compression", "code", "error_type"}
)


def _user_names(value: Any, out: set[str], key: str = "") -> None:
    """Names in a fingerprint: string values (not format facts or hashes) and the keys of maps
    named by the user."""
    if isinstance(value, str):
        if key not in _FACT_KEYS and not _HASH.match(value):
            out.add(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            if key in _NAMED_KEYS:
                out.add(str(k))
            _user_names(v, out, str(k))
    elif isinstance(value, list):
        for v in value:  # items of a list are not keys of a named map
            _user_names(v, out, key if isinstance(v, str) else "")


def known_names(cfg: Config) -> set[str]:
    """Names that must never appear in a bundle: roots and their paths, every file, folder and
    name in the index, the user and the host, the configured folders and host names."""
    names: set[str] = set()
    for r in cfg.data_roots:
        names.add(r.name)
        names.add(str(r.path))
    for p in (cfg.cache_dir, cfg.state, cfg.logging.dir, cfg.jobs.tmp_dir):
        if p is not None:
            names.add(str(p))
    names.update(h for h in cfg.server.allowed_hosts if h not in ("localhost", "127.0.0.1", "*"))
    for value in (getpass.getuser(), socket.gethostname(), str(Path.home())):
        names.add(value)
    for row in _sql(cfg.state / "catalog.sqlite", "SELECT relpath, fingerprint FROM datasets"):
        names.update(Path(row["relpath"]).parts)
        if row["fingerprint"]:
            _user_names(json.loads(row["fingerprint"]), names)
    # very short or format-defined names would match everywhere: not names of the user's
    from canopy.privacy import KEEP

    return {
        n
        for n in names
        if len(n) >= 4 and n not in KEEP and not n.isdigit() and not _VERSION.fullmatch(n)
    }


GENERIC = frozenset(
    {
        "data",
        "obs",
        "var",
        "obsm",
        "uns",
        "layers",
        "counts",
        "cache",
        "logs",
        "true",
        "false",
        "none",
        "null",
        "spatial",
        "images",
        "labels",
        "shapes",
        "points",
        "tables",
        "table",
        "categorical",
        "numeric",
        "string",
        "float32",
        "float64",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "h5ad",
        "zarr",
        "csr",
        "csc",
        "dense",
        "region",
        "instance_id",
        "cell_id",
        "_index",
    }
)


# Fields whose values are the program's own vocabulary (event and stage names, states, codes,
# formats): a dataset column that happens to be called "stage" is not found in them.
CODE_FIELDS = frozenset(
    {
        "event",
        "logger",
        "level",
        "role",
        "stage",
        "kind",
        "code",
        "reason",
        "state",
        "error",
        "mode",
        "encoding",
        "x_encoding",
        "layout",
        "dtype",
        "status",
        "route",
        "method",
        "trigger",
        "type",
        "client_event",
        "c_type",
        "c_mode",
        "c_status",
        "c_layer",
        "x_kind",
        "y_kind",
        "c_where",
        "category",
        "location",
        "exc_type",
        "exc_frames",
        "warnings",
        "build",
        "timestamp",
        "what",
    }
)


def _values(value: Any, out: list[str], key: str = "") -> None:
    """String values of a JSON document outside the program's own vocabulary fields. An event
    that is not a plain code (a library's message) is searched too."""
    if key == "event" and isinstance(value, str) and not _CODE.fullmatch(value):
        out.append(value)
        return
    if key in CODE_FIELDS:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            _values(v, out, str(k))
    elif isinstance(value, list):
        for v in value:
            _values(v, out, key)


def _searchable(fname: str, data: bytes) -> str:
    text = data.decode(errors="replace")
    if not (fname.endswith(".json") or fname.endswith(".jsonl")):
        return text
    vals: list[str] = []
    docs = [text] if fname.endswith(".json") else text.splitlines()
    for d in docs:
        try:
            _values(json.loads(d), vals)
        except ValueError:
            vals.append(d)
    return "\n".join(vals)


def self_check(files: dict[str, bytes], names: set[str]) -> list[tuple[str, str]]:
    """(file in the bundle, name) for every known name found in it: in plain files anywhere, in
    JSON only in values that are not the program's own vocabulary."""
    hits = []
    pats = [n for n in names if n.lower() not in GENERIC]
    for fname, data in files.items():
        text = _searchable(fname, data)
        for n in pats:
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", text):
                hits.append((fname, n))
    return hits


class BundleRefusedError(RuntimeError):
    """The self-check found known names; `hits` are (file, name) pairs, for the owner's terminal
    only (the message names the files, never the names)."""

    def __init__(self, hits: list[tuple[str, str]]) -> None:
        where = sorted({f for f, _ in hits})
        super().__init__(
            f"a name known on this host was found in {len(hits)} place(s) in: "
            f"{', '.join(where)}; the bundle was not written"
        )
        self.hits = hits


def build_bundle(cfg: Config, out: Path, since_days: float) -> tuple[Path, list[str]]:
    """Write the archive; returns (path, notes). Raises BundleRefusedError when the self-check
    finds a known name."""
    salt = installation_salt(cfg.state)
    cutoff = time.time() - since_days * 86400
    log_dirs = [d for d in (cfg.logging.dir, default_log_dir()) if d.is_dir()]
    files: dict[str, bytes] = {}
    notes: list[str] = []
    for d in log_dirs:
        for f in sorted(d.iterdir()):
            if not f.is_file() or f.stat().st_mtime < cutoff:
                continue
            if f.name.startswith("canopy-") and ".jsonl" in f.name:
                text, dropped = _log_lines(f, salt)
                files[f"logs/{f.name}"] = text.encode()
                if dropped:
                    notes.append(f"{f.name}: {dropped} non-JSON lines left out")
            elif f.name.startswith("stacks-") or f.name.endswith(".stderr.txt"):
                files[f"crash/{f.name}"] = _code_only(f.read_text(errors="replace")).encode()
    files["host.json"] = json.dumps(_host_facts(cfg, salt), indent=2, default=str).encode()
    files["jobs.json"] = json.dumps(_jobs(cfg, salt), indent=2, default=str).encode()
    files["catalog.json"] = json.dumps(_catalog(cfg, salt), indent=2, default=str).encode()
    created = datetime.now(UTC).isoformat(timespec="seconds")
    manifest = [
        "# Canopy diagnostics bundle",
        "",
        f"Created {created}, logs of the last {since_days:g} days, privacy: strict.",
        "",
        "| File | What |",
        "|---|---|",
    ]
    for name in sorted(files):
        what = (
            "log lines (names as tokens, free text dropped)"
            if name.startswith("logs/")
            else "code locations of a crash, hang or worker error"
            if name.startswith("crash/")
            else {
                "host.json": "versions, CPU, memory, file systems, cgroup limits",
                "jobs.json": "conversion jobs: states, error codes, timings, sizes",
                "catalog.json": "index: kinds, states, reason codes, sizes (ids as tokens)",
            }.get(name, "")
        )
        manifest.append(f"| `{name}` | {what} |")
    manifest += ["", "Tokens (h_…) can be looked up on the host with `canopy token find`."]
    files["MANIFEST.md"] = ("\n".join(manifest) + "\n").encode()
    hits = self_check(files, known_names(cfg))
    if hits:
        raise BundleRefusedError(hits)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"canopy-bundle/{name}")
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    os.chmod(out, 0o600)
    return out, notes
