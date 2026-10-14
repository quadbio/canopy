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
    r = CliRunner().invoke(
        app, ["bundle", "-c", str(_any_file(tmp_path)), "--since-days", "1", "--out", out]
    )
    assert r.exit_code == 1 and "LEAKEDROOT" not in r.output and "--show-names" in r.output
    r = CliRunner().invoke(
        app,
        [
            "bundle",
            "-c",
            str(_any_file(tmp_path)),
            "--since-days",
            "1",
            "--out",
            out,
            "--show-names",
        ],
    )
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


def test_common_words_as_names_do_not_refuse_the_bundle(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Columns called like words of Canopy's own text (the manifest, host.json, a function in a
    crash file) are names, but that text is the program's: the bundle is written (review A-H1)."""
    cols = {c: {} for c in ("index", "memory", "error", "sizes", "states", "tokens", "summary")}
    fp = {"columns": cols}
    rows = [{"relpath": "a/b.h5ad", "fingerprint": json.dumps(fp)}]
    monkeypatch.setattr(bundle_mod, "_sql", lambda db, q: rows if "fingerprint" in q else [])
    setup_logging(cfg.logging, role="server", salt="x", to_console=False)
    (cfg.logging.dir / "stacks-1.txt").write_text(
        'Thread 0x01 (most recent call first):\n  File "/x/site-packages/canopy/data/api.py", '
        "line 805 in summary\n"
    )
    out, _ = build_bundle(cfg, tmp_path / "b.tar.gz", since_days=1)
    assert out.exists()


def test_settings_at_start_up_do_not_match_folders_called_like_them(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start-up line logs `file_access="private"` and `auth="token"`: folders called so are
    names, the settings are not (host test 3). Empty crash files are left out."""
    rows = [
        {"relpath": "private/a.h5ad", "fingerprint": None},
        {"relpath": "token/b.h5ad", "fingerprint": None},
    ]
    monkeypatch.setattr(bundle_mod, "_sql", lambda db, q: rows if "fingerprint" in q else [])
    assert {"private", "token"} <= known_names(cfg)
    setup_logging(cfg.logging, role="server", salt="x", to_console=False)
    with TestClient(create_app(cfg, background=False)):
        pass
    (cfg.logging.dir / "stacks-7.txt").write_text("")
    out, _ = build_bundle(cfg, tmp_path / "b.tar.gz", since_days=1)
    with tarfile.open(out) as tar:
        names = tar.getnames()
        manifest = tar.extractfile("canopy-bundle/MANIFEST.md").read().decode()  # type: ignore[union-attr]
    assert not any("stacks-7" in n for n in names) and "stacks-7" not in manifest


def test_dated_and_versioned_names_stay_names(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a bare version number from a file's contents is not a name; a dated folder is (review
    A-H2), and so are a folder or a host called like a version or an address (H3 A-L2)."""
    fp = {"uns": {"tool_version": "0.8.0"}}
    rows = [
        {
            "relpath": "2024.03.12-patient07/v2.1-lab/2024.03.12/x.h5ad",
            "fingerprint": json.dumps(fp),
        }
    ]
    monkeypatch.setattr(bundle_mod, "_sql", lambda db, q: rows)
    cfg.server.allowed_hosts = ["10.20.30.40"]
    names = known_names(cfg)
    assert {"2024.03.12-patient07", "v2.1-lab", "2024.03.12", "10.20.30.40"} <= names
    assert "0.8.0" not in names
    # and the check ignores case (tools lower-case names)
    assert self_check({"logs/s.jsonl": b'{"event":"odd","x":"V2.1-LAB"}'}, names)
    leak = {"event": "odd", "detail_path": "2024.03.12-patient07"}
    assert self_check({"logs/s.jsonl": json.dumps(leak).encode()}, names)
    # a vocabulary field holding something other than a plain code is searched too (A-L5)
    odd = {"event": "odd", "kind": "2024.03.12-patient07"}
    assert self_check({"logs/s.jsonl": json.dumps(odd).encode()}, names)


def test_owner_can_let_a_harmless_name_through(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_logging(cfg.logging, role="server", salt="x", to_console=False)
    get_logger("canopy.test").info("odd", unknown_field="LEAKEDROOT")
    cfg.data_roots[0].name = "LEAKEDROOT"
    monkeypatch.setattr(cli_mod, "_load", lambda config, **kw: cfg)
    out = tmp_path / "b.tar.gz"
    r = CliRunner().invoke(
        app,
        [
            "bundle",
            "-c",
            str(_any_file(tmp_path)),
            "--since-days",
            "1",
            "--out",
            str(out),
            "--accept-name",
            "LEAKEDROOT",
        ],
    )
    assert r.exit_code == 0, r.output
    with tarfile.open(out) as tar:
        manifest = tar.extractfile("canopy-bundle/MANIFEST.md").read().decode()  # type: ignore[union-attr]
    assert "1 name(s) known on the host were let through" in manifest
    assert "LEAKEDROOT" not in manifest


def _any_file(tmp_path: Path) -> Path:
    """A config path (the tests replace loading it)."""
    p = tmp_path / "canopy.toml"
    p.touch()
    return p


def test_the_programs_own_text_does_not_refuse_the_bundle(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Columns called like the app's script files ("index"), a library's message ("slice"), the
    browser or GPU wording, or a fault header ("error", "recent") are names; those texts are the
    program's and are not searched (review H3 A-M1). A worker's error output with no code
    location in it (a warning only) is no crash file (H3 A-L1)."""
    cols = {c: {} for c in ("index", "expression", "slice", "error", "recent", "Metal", "Chrome")}
    rows = [{"relpath": "a/b.h5ad", "fingerprint": json.dumps({"columns": cols})}]
    monkeypatch.setattr(bundle_mod, "_sql", lambda db, q: rows if "fingerprint" in q else [])
    setup_logging(cfg.logging, role="server", salt="x", to_console=False)
    from canopy.telemetry import LIBRARY_MESSAGES

    with TestClient(create_app(cfg, background=False)) as c:
        ev = [
            {
                "name": "js_error",
                "fields": {
                    "type": "Error",
                    "message": "slice step cannot be zero",
                    "where": "index-Bkj0RbvK.js:1:2345",
                    "caller": "expression-Bl_GL2tO.js:3:4 index-Bkj0RbvK.js:5:6",
                },
            },
            {
                "name": "environment",
                "fields": {"browser": "Chrome 154", "gpu": "ANGLE (Apple Metal Renderer)"},
            },
        ]
        assert c.post("/api/telemetry", json={"session": "s1", "events": ev}).status_code == 204
    assert "slice step cannot be zero" in LIBRARY_MESSAGES
    (cfg.logging.dir / "stacks-1.txt").write_text(
        "Fatal Python error: Segmentation fault\n\nCurrent thread 0x01 (most recent call first):\n"
        '  File "/x/site-packages/h5py/_hl/files.py", line 12 in read\nExtension modules: numpy\n'
    )
    (cfg.logging.dir / "canopy-worker-job1.stderr.txt").write_text("UserWarning: something\n")
    out, _ = build_bundle(cfg, tmp_path / "b.tar.gz", since_days=1)
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert "canopy-bundle/crash/stacks-1.txt" in names
    assert not any("stderr" in n for n in names)


def test_bundle_link_and_token_find_need_the_installations_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without one, the bundle's check would know no dataset names, and `link` would make a new
    token in ./cache (review H3 A-M2)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CANOPY_CONFIG", raising=False)
    for args in (["bundle"], ["link"], ["token", "find", "h_0123456789"]):
        r = CliRunner().invoke(app, args)
        assert r.exit_code == 2 and "--config" in r.output, args
    assert not (tmp_path / "cache").exists()
