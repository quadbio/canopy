"""canopy selfcheck (an installation's readiness) and canopy analyse (a bundle as a report)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

import canopy.bundle as bundle_mod
from canopy.analyse import analyse
from canopy.bundle import build_bundle
from canopy.cli import app
from canopy.config import Config
from canopy.logs import setup_logging
from canopy.selfcheck import run


def _conf(cfg: Config, path: Path, relative: bool = False) -> Path:
    cache = "cache" if relative else str(cfg.cache_dir)
    path.write_text(
        f'cache_dir = "{cache}"\n[[data_roots]]\nname = "data"\npath = "{cfg.data_roots[0].path}"\n'
        f'[logging]\ndir = "{cfg.logging.dir}"\n'
    )
    return path


def test_selfcheck_reports_each_area_and_logs_no_names(cfg: Config, tmp_path: Path) -> None:
    (cfg.data_roots[0].path / "SECRETDIR").mkdir()
    (cfg.data_roots[0].path / "SECRETDIR" / "SECRETFILE.h5ad").write_bytes(b"\0" * (3 * 2**20))
    conf = _conf(cfg, tmp_path / "canopy.toml")
    from canopy.config import MemoryConfig

    cfg.memory = MemoryConfig(budget_gb=1, cache_gb=0.5)  # fits any test machine (4 GB trial)
    setup_logging(cfg.logging, role="cli", salt="x", to_console=False)
    checks = run(cfg, conf, read_mb=2)
    areas = {c.area for c in checks}
    assert {"config", "folder", "memory", "data_root", "port", "index"} <= areas
    root = next(c for c in checks if c.area == "data_root")
    assert root.status == "ok" and root.facts["read_mb"] == 2 and "random_64k_ms" in root.facts
    assert not any(c.status == "fail" for c in checks), [c.message for c in checks]
    logs = "".join(p.read_text() for p in cfg.logging.dir.glob("*.jsonl"))
    assert '"selfcheck"' in logs and "SECRET" not in logs and str(tmp_path) not in logs
    # what it logged passes the bundle's check for names
    out, _ = build_bundle(cfg, tmp_path / "b.tar.gz", since_days=1)
    assert out.exists()


def test_selfcheck_warns_on_relative_folders_and_fails_on_an_impossible_budget(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = _conf(cfg, tmp_path / "canopy.toml", relative=True)
    monkeypatch.setattr("canopy.selfcheck.cgroup_memory_limit", lambda: {"limit_bytes": 4 * 2**30})
    cfg.memory.budget_gb = 100
    checks = run(cfg, conf, read_mb=0)
    assert next(c for c in checks if c.area == "config").status == "warn"
    assert next(c for c in checks if c.area == "memory").status == "fail"
    r = CliRunner().invoke(app, ["selfcheck", "--read-mb", "0"])
    assert r.exit_code == 2 and "--config" in r.output


def _bundle(path: Path, logs: list[dict[str, object]], jobs: list[dict[str, object]]) -> Path:
    files = {
        "MANIFEST.md": "# Canopy diagnostics bundle\n\nCreated 2026-10-14T07:51:32+00:00, logs.\n",
        "host.json": json.dumps({"environment": {"cpu_count": 8, "memory_total_gb": 64}}),
        "jobs.json": json.dumps(jobs),
        "catalog.json": json.dumps(
            [{"kind": "h5ad", "state": "inspected", "size_bytes": 2**30, "inspect_ms": 50}]
        ),
        "logs/canopy-server-1.jsonl": "\n".join(json.dumps(e) for e in logs) + "\nnot json\n",
        "crash/stacks-1.txt": "",
        "crash/stacks-2.txt": '  File "canopy/x.py", line 3 in f\n',
    }
    with tarfile.open(path, "w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"canopy-bundle/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _client(name: str, ms: float | None, **f: object) -> dict[str, object]:
    return {
        "event": "client_event",
        "client_event": name,
        "ms": ms,
        "timestamp": "2026-10-14T07:00:00Z",
        "build": "0.0.1+gabc",
        **{f"c_{k}": v for k, v in f.items()},
    }


def test_analyse_reports_problems_targets_and_the_change_from_before(tmp_path: Path) -> None:
    logs = [
        {
            "event": "startup",
            "role": "server",
            "timestamp": "2026-10-14T06:00:00Z",
            "build": "0.0.1+gabc",
        },
        {"event": "request", "route": "/api/datasets/{id}/gene", "ms": 40, "status": 200},
        {"event": "request", "route": "/api/datasets/{id}/meta", "ms": 900, "status": 500},
        {
            "event": "stage_failed",
            "level": "error",
            "stage": "load_array",
            "exc_type": "OSError",
            "exc_frames": ["a.py:1:f"],
        },
        _client(
            "js_unhandled_rejection",
            None,
            message="Input contains an empty iterator.",
            where="i.js:1:2",
            caller="i.js:3:4",
        ),
        _client("view_ready", 2000, type="embedding", n=1_000_000),
        _client("gene_loaded", 200, n=50_000, linked=0),
        _client("gene_loaded", 1500, n=5_000, linked=1),  # a linked group's member: apart
        _client("frames", None, n_moving=40, moving_median_ms=50, moving_p95_ms=80, views=19),
    ]
    jobs = [
        {
            "id": 1,
            "state": "done",
            "created": 0,
            "started": 5,
            "result": {"seconds": 30, "peak_rss_gb": 2},
        },
        {
            "id": 2,
            "state": "failed",
            "error": "not_enough_memory",
            "created": 0,
            "started": 1,
            "result": None,
        },
    ]
    now = _bundle(tmp_path / "now.tar.gz", logs, jobs)
    before = _bundle(tmp_path / "before.tar.gz", logs[:2], jobs[:1])
    text = analyse(now, before)
    assert "1 server errors (5xx)" in text
    assert "stacks-2.txt" in text and "stacks-1.txt" not in text  # empty crash files are no news
    assert "Conversions not done" in text and "not_enough_memory" in text
    assert "Input contains an empty iterator. at i.js:1:2 (from i.js:3:4)" in text
    assert (
        "| Embedding of ≥ 0.5 M cells ready (90 % of views) | 3000 ms | 2000 ms | 1 | met |" in text
    )
    assert (
        "| Gene colouring, ≤ 1.2 M cells (90 % of switches) | 300 ms | 200 ms | 1 | met |" in text
    )
    assert "Genes loaded in linked groups" in text
    assert (
        "| Frame while moving (median of drags; ≥ 30 frames/s) | 33 ms | 50 ms | 1 | **missed** |"
        in text
    )
    assert (
        "## Compared with the previous bundle" in text
        and "| Conversions (done / all) | 1 / 1 | 1 / 2 |" in text
    )
    assert "1 log lines could not be read" in text
    out = tmp_path / "r.md"
    r = CliRunner().invoke(app, ["analyse", str(now), "--out", str(out)])
    assert r.exit_code == 0 and out.read_text() == analyse(now)


def test_selfcheck_events_pass_the_bundle_check(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dataset folder called like a folder code or a file system ("state", "xfs") is a name;
    the selfcheck's own fields are not searched for it."""
    rows = [{"relpath": "state/xfs/a.h5ad", "fingerprint": None}]
    monkeypatch.setattr(bundle_mod, "_sql", lambda db, q: rows if "fingerprint" in q else [])
    setup_logging(cfg.logging, role="cli", salt="x", to_console=False)
    from canopy.logs import get_logger

    get_logger("canopy.selfcheck").info(
        "selfcheck", area="folder", status="ok", what="state", fstype="xfs"
    )
    out, _ = build_bundle(cfg, tmp_path / "b.tar.gz", since_days=1)
    assert out.exists()
