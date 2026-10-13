"""canopy bundle: strict re-filtering, crash files as code locations, the self-check, and
start-up failure records."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import anndata as ad
import numpy as np
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import canopy.bundle as bundle_mod
import canopy.cli as cli_mod
from canopy.app import create_app
from canopy.bundle import build_bundle, known_names, self_check
from canopy.cli import app
from canopy.config import Config
from canopy.logs import get_logger, setup_logging


def test_bundle_is_strict_whatever_the_run_used(cfg: Config, tmp_path: Path) -> None:
    cfg.logging.privacy = "standard"  # the run kept names: the bundle must not
    (cfg.data_roots[0].path / "SECRETDIR").mkdir()
    a = ad.AnnData(X=np.zeros((3, 2), np.float32))
    a.obs["SECRETCOLUMN"] = ["a", "b", "c"]
    a.write_h5ad(cfg.data_roots[0].path / "SECRETDIR" / "SECRETFILE.h5ad")
    setup_logging(cfg.logging, role="server", salt="x", to_console=False)
    with TestClient(create_app(cfg, background=False)) as c:
        c.app.state.catalog.scan_now()  # type: ignore[attr-defined]
        c.get("/api/catalog/search", params={"q": "SECRETFILE"})
    get_logger("canopy.test").error("boom", exc_info=ValueError("SECRETWORD in a message"))
    (cfg.logging.dir / "stacks-1.txt").write_text(
        "Fatal Python error: Segmentation fault\n\nThread 0x01 (most recent call first):\n"
        '  File "/home/SECRETUSER/env/lib/python3.14/site-packages/h5py/_hl/files.py", '
        "line 12 in read\n"
        "SECRETWORD garbage\n"
    )
    out, _ = build_bundle(cfg, tmp_path / "b.tar.gz", since_days=1)
    with tarfile.open(out) as tar:
        names = tar.getnames()
        text = "".join(tar.extractfile(m).read().decode() for m in tar.getmembers() if m.isfile())  # type: ignore[union-attr]
    assert "canopy-bundle/MANIFEST.md" in names and "canopy-bundle/jobs.json" in names
    assert "SECRET" not in text and str(tmp_path) not in text
    assert "h5py/_hl/files.py:12" in text  # the crash's code location is kept


@pytest.mark.parametrize("privacy", ["standard", "debug"])
def test_library_log_text_does_not_reach_the_bundle(
    cfg: Config, tmp_path: Path, privacy: str
) -> None:
    """A library's own log message (free text, possibly naming a column) is kept by a
    standard or debug run; the bundle drops it and keeps that a line was logged."""
    import logging

    cfg.logging.privacy = privacy  # type: ignore[assignment]
    setup_logging(cfg.logging, role="server", salt="x", to_console=False)
    logging.getLogger("numcodecs.some").warning("column patient_cohort of secret_study is odd")
    out, _ = build_bundle(cfg, tmp_path / "b.tar.gz", since_days=1)
    with tarfile.open(out) as tar:
        text = "".join(tar.extractfile(m).read().decode() for m in tar.getmembers() if m.isfile())  # type: ignore[union-attr]
    assert "patient_cohort" not in text and "secret_study" not in text
    assert "dropped_library_text" in text and "numcodecs.some" in text


def test_bundle_refuses_when_a_known_name_slips_through(cfg: Config, tmp_path: Path) -> None:
    setup_logging(cfg.logging, role="server", salt="x", to_console=False)
    # a field the filter does not know, holding a root name
    get_logger("canopy.test").info(
        "odd", unknown_field=cfg.data_roots[0].name + "_x " + "LEAKEDROOT"
    )
    cfg.data_roots[0].name = "LEAKEDROOT"
    with pytest.raises(RuntimeError, match="not written"):
        build_bundle(cfg, tmp_path / "b.tar.gz", since_days=1)
    assert not (tmp_path / "b.tar.gz").exists()


def test_version_numbers_and_program_fields_are_not_taken_for_names(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SpatialData store records the spatialdata version that wrote it; the start-up line
    lists the installed one. A var index called "gene" meets the log field `what: gene`. Neither
    is a name: found on the first host test."""
    fp = {"software_version": "0.8.0", "index_name": "gene", "columns": {"cohort_x": {}}}
    monkeypatch.setattr(
        bundle_mod, "_sql", lambda db, q: [{"relpath": "a/b.zarr", "fingerprint": json.dumps(fp)}]
    )
    names = known_names(cfg)
    assert "0.8.0" not in names and "cohort_x" in names
    line = {"event": "startup", "packages": {"spatialdata": "0.8.0"}, "what": "gene"}
    assert self_check({"logs/s.jsonl": json.dumps(line).encode()}, names) == []
    leak = {"event": "odd", "unknown_field": "cohort_x"}
    assert self_check({"logs/s.jsonl": json.dumps(leak).encode()}, names) == [
        ("logs/s.jsonl", "cohort_x")
    ]


def test_refused_bundle_shows_names_only_when_asked(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_logging(cfg.logging, role="server", salt="x", to_console=False)
    get_logger("canopy.test").info("odd", unknown_field="LEAKEDROOT")
    cfg.data_roots[0].name = "LEAKEDROOT"
    monkeypatch.setattr(cli_mod, "_load", lambda config, **kw: cfg)
    out = str(tmp_path / "b.tar.gz")
    r = CliRunner().invoke(app, ["bundle", "--since-days", "1", "--out", out])
    assert r.exit_code == 1 and "LEAKEDROOT" not in r.output and "--show-names" in r.output
    r = CliRunner().invoke(app, ["bundle", "--since-days", "1", "--out", out, "--show-names"])
    assert r.exit_code == 1 and "LEAKEDROOT" in r.output
    assert not (tmp_path / "b.tar.gz").exists()


def test_startup_failures_reach_the_log_folder(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    bad = tmp_path / "bad.toml"
    bad.write_text(f'[logging]\ndir = "{logs}"\n[server\nport = 1\n')  # syntax error
    r = CliRunner().invoke(app, ["serve", "--config", str(bad)])
    assert r.exit_code == 2
    records = [
        json.loads(x)
        for f in logs.glob("canopy-startup-*.jsonl")
        for x in f.read_text().splitlines()
    ]
    assert records and records[0]["code"] == "config_syntax" and records[0]["line"] == 3
