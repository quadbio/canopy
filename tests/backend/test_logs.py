from __future__ import annotations

import json
import logging
import os
import re
import time
import warnings
from pathlib import Path

import pytest
from conftest import log_events

from canopy.build import build_id
from canopy.config import Config, LoggingConfig
from canopy.logs import (
    current_log_file,
    get_logger,
    log_file_name,
    memory_snapshot,
    prune_logs,
    setup_logging,
    timed,
)
from canopy.privacy import redact_text, token

SECRET_NAME = "patient_0042_tumour"
SECRET_DIR = "/home/alice/secret_project/cohort7"


def test_timed_logs_stage_and_extra_fields(cfg: Config) -> None:
    with timed("read_x", dataset="d1") as extra:
        extra["bytes"] = 123
    ev = log_events()[-1]
    assert ev["event"] == "stage"
    assert ev["stage"] == "read_x"
    assert ev["dataset"] == "d1"
    assert ev["bytes"] == 123
    assert ev["ms"] >= 0
    assert ev["rss_peak_mb"] >= ev["rss_mb"] - 1 and ev["rss_peak_process_mb"] > 0


def test_timed_nests_and_reports_peak_memory(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    rss = [100.0]
    monkeypatch.setattr("canopy.logs.rss_mb", lambda: rss[0])
    with timed("convert"):
        with timed("x_csc"):
            rss[0] = 500.0  # a short spike, gone before the stage ends
            time.sleep(0.2)
            rss[0] = 120.0
        with timed("write"):
            pass
    stages = {e["stage"]: e for e in log_events() if e["event"] == "stage"}
    assert set(stages) >= {"convert", "convert/x_csc", "convert/write"}
    x = stages["convert/x_csc"]
    assert (x["rss_peak_mb"], x["rss_mb"], x["rss_delta_mb"]) == (500.0, 120.0, 20.0)
    assert stages["convert"]["rss_peak_mb"] == 500.0
    assert stages["convert/write"]["rss_peak_mb"] == 120.0
    with timed("after"):
        pass
    assert log_events()[-1]["stage"] == "after"  # the path was reset


def test_timed_logs_failure_and_reraises(cfg: Config) -> None:
    with pytest.raises(ValueError), timed("outer"), timed("boom"):
        raise ValueError("x")
    ev = next(e for e in log_events() if e["event"] == "stage_failed")
    assert ev["stage"] == "outer/boom"
    assert ev["exc_type"] == "ValueError"
    assert any("test_logs.py" in f for f in ev["exc_frames"])


def test_timed_logs_an_expected_refusal_as_info(cfg: Config) -> None:
    """A refusal answered with a code (too many values, needs conversion) is not a fault."""
    from canopy.data.reader import NeedsConversionError

    with pytest.raises(NeedsConversionError), timed("load_array", what="obs"):
        raise NeedsConversionError("too_many_values")
    events = log_events()
    ev = next(e for e in events if e["event"] == "stage_refused")
    assert ev["level"] == "info" and ev["exc_type"] == "NeedsConversionError"
    assert "exc_frames" not in ev
    assert not any(e["event"] == "stage_failed" for e in events)


def test_every_line_has_pid_role_build(cfg: Config) -> None:
    get_logger("x").info("hello")
    f = current_log_file()
    assert f is not None and f.name == log_file_name("test") == f"canopy-test-{os.getpid()}.jsonl"
    for ev in log_events():
        assert ev["pid"] == os.getpid() and ev["role"] == "test" and ev["build"] == build_id()


def test_build_id_has_commit() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+.*\+g[0-9a-f]{10}", build_id()), build_id()


def _fail_everywhere() -> None:
    """Secret names through every logging path: fields, exceptions, chains, warnings,
    third-party log lines."""
    log = get_logger("canopy.test")
    log.info("dataset_opened", path=f"{SECRET_DIR}/{SECRET_NAME}.h5ad", column=SECRET_NAME)
    log.info("nested", config={"data_roots": [{"name": SECRET_NAME, "path": SECRET_DIR}]})
    try:
        try:
            raise KeyError(SECRET_NAME)
        except KeyError as e:
            raise ValueError(f"cannot convert '{SECRET_NAME}' in {SECRET_DIR}/x.h5ad") from e
    except ValueError:
        log.exception("convert_failed")
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        # pytest replaces warnings.showwarning for each test; hook logging in again.
        logging.captureWarnings(False)
        logging.captureWarnings(True)
        warnings.warn(f"column '{SECRET_NAME}' has mixed types", UserWarning, stacklevel=1)
    logging.getLogger("thirdparty").error("could not read %s", f"{SECRET_DIR}/{SECRET_NAME}")


def test_strict_privacy_leaves_no_names(tmp_path: Path) -> None:
    setup_logging(
        LoggingConfig(dir=tmp_path, privacy="strict"), role="test", salt="s", to_console=False
    )
    _fail_everywhere()
    text = (tmp_path / log_file_name("test")).read_text()
    for secret in (SECRET_NAME, "alice", "secret_project", "cohort7"):
        assert secret not in text
    events = log_events()
    failed = next(e for e in events if e["event"] == "convert_failed")
    assert failed["exc_type"] == "ValueError" and "exc_message" not in failed
    assert failed["exc_chain"] == [{"exc_type": "KeyError"}]
    assert failed["exc_frames"]
    opened = next(e for e in events if e["event"] == "dataset_opened")
    assert opened["column"] == token(SECRET_NAME, "s")
    warning = next(e for e in events if e["event"] == "python_warning")
    assert warning["category"] == "UserWarning" and "message" not in warning


def test_standard_privacy_tokenises_quoted_text_and_paths(tmp_path: Path) -> None:
    setup_logging(LoggingConfig(dir=tmp_path), role="test", salt="s", to_console=False)
    _fail_everywhere()
    events = log_events()
    failed = next(e for e in events if e["event"] == "convert_failed")
    assert failed["exc_message"].startswith("cannot convert '" + token(SECRET_NAME, "s"))
    assert "alice" not in failed["exc_message"]
    warning = next(e for e in events if e["event"] == "python_warning")
    assert SECRET_NAME not in warning["message"] and "mixed types" in warning["message"]
    third = next(e for e in events if e.get("logger") == "thirdparty")
    assert "alice" not in third["event"]
    # names in name fields are kept at standard
    assert next(e for e in events if e["event"] == "dataset_opened")["column"] == SECRET_NAME


def test_debug_privacy_keeps_everything(tmp_path: Path) -> None:
    setup_logging(
        LoggingConfig(dir=tmp_path, privacy="debug"), role="test", salt="s", to_console=False
    )
    _fail_everywhere()
    failed = next(e for e in log_events() if e["event"] == "convert_failed")
    assert SECRET_NAME in failed["exc_message"] and "Traceback" in failed["exception"]


def test_redact_text_limits_length() -> None:
    assert len(redact_text("x" * 1000, "standard", "s") or "") <= 301
    assert redact_text("anything", "strict", "s") is None


def test_prune_removes_only_old_log_files(tmp_path: Path) -> None:
    old = tmp_path / "canopy-server-1.jsonl"
    new = tmp_path / "canopy-worker-2.jsonl.1"
    other = tmp_path / "notes.txt"
    for f in (old, new, other):
        f.write_text("x")
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert prune_logs(tmp_path, 30) == 1
    assert not old.exists() and new.exists() and other.exists()


def test_memory_snapshot_fields(cfg: Config) -> None:
    snap = memory_snapshot()
    assert snap["rss_mb"] > 0
    assert snap["host_total_gb"] > 0


def test_strict_server_log_has_no_names(tmp_path: Path) -> None:
    """End to end: a server in strict mode, with marker names in roots, folders, files, columns
    and config, exercised through every endpoint and its error paths."""
    import anndata as ad
    import numpy as np
    import pandas as pd
    from fastapi.testclient import TestClient

    from canopy.app import create_app
    from canopy.catalog import entry_id
    from canopy.config import CatalogConfig, Config, DataRoot, ServerConfig

    data = tmp_path / "SECRETROOT"
    (data / "SECRETFOLDER").mkdir(parents=True)
    a = ad.AnnData(X=np.zeros((4, 2), np.float32))
    a.obs["SECRETCOLUMN"] = pd.Categorical(["SECRETVALUE", "b", "b", "SECRETVALUE"])
    a.obsm["X_umap"] = np.zeros((4, 2), np.float32)
    a.var_names = ["SECRETGENE1", "SECRETGENE2"]
    a.layers["SECRETLAYER"] = np.ones((4, 2), np.float32)
    a.write_h5ad(data / "SECRETFOLDER" / "SECRETFILE.h5ad")
    (data / "SECRETBROKEN.h5ad").write_bytes(b"\x89HDF\r\n\x1a\n" + b"\0" * 64)
    cfg = Config(
        data_roots=[DataRoot(name="SECRETNAME", path=data)],
        cache_dir=tmp_path / "cache",
        server=ServerConfig(allowed_hosts=["testserver"], auth="none"),
        logging=LoggingConfig(dir=tmp_path / "logs", privacy="strict", memory_sample_interval=0),
        catalog=CatalogConfig(ignore=["SECRETIGNORE*"]),
    )
    setup_logging(cfg.logging, role="test", salt="s", to_console=False)
    get_logger("canopy.config").info("config_loaded", config=cfg.model_dump(mode="json"))
    app = create_app(cfg, background=False)
    app.state.catalog.scan_now()
    id = entry_id("SECRETNAME", "SECRETFOLDER/SECRETFILE.h5ad")
    with TestClient(app) as c:
        c.get("/api/catalog/roots")
        c.get("/api/catalog/tree", params={"root": "SECRETNAME", "path": "SECRETFOLDER"})
        c.get("/api/catalog/search", params={"q": "SECRETFILE"})
        c.get(f"/api/datasets/{id}/meta")
        c.get(f"/api/datasets/{id}/obs", params={"column": "SECRETCOLUMN"})
        c.get(f"/api/datasets/{id}/obs", params={"column": "SECRETMISSING"})
        c.get(f"/api/datasets/{id}/obsm", params={"key": "SECRETKEY"})
        c.get(f"/api/datasets/{id}/genes")
        c.get(f"/api/datasets/{id}/gene", params={"i": 1, "matrix": "SECRETLAYER", "q": 16})
        c.get(f"/api/datasets/{id}/gene", params={"i": 0, "matrix": "SECRETNOPE"})
        c.post(
            "/api/telemetry",
            json={"session": "s", "events": [{"name": "x", "fields": {"label": "SECRETTEL"}}]},
        )
    text = (tmp_path / "logs" / log_file_name("test")).read_text()
    leaks = [w for w in ("SECRET", str(tmp_path)) if w in text]
    assert leaks == [], [line for line in text.splitlines() if "SECRET" in line][:3]
    # the dataset id is a plain hash of root and path: at strict it is a salted token, the same
    # on every line, so requests still link to the inspection of their file
    assert id not in text and token(id, "s") in text
    lines = [json.loads(x) for x in text.splitlines()]
    inspected = next(e for e in lines if e.get("event") == "dataset_inspected" and e.get("kind"))
    assert inspected["dataset_id"] == token(id, "s") and inspected["version"]
    assert {"n_obs", "n_vars", "x_encoding"} <= set(inspected)


def test_io_errors_keep_their_errno_at_every_level(tmp_path: Path) -> None:
    from canopy.privacy import redact_text

    for level in ("strict", "standard"):
        setup_logging(
            LoggingConfig(dir=tmp_path / level, privacy=level),
            role="test",
            salt="s",
            to_console=False,
        )
        log = get_logger("canopy.test")
        try:
            raise OSError(5, "Input/output error")
        except OSError:
            log.error("read_failed", exc_info=True)
        try:  # h5py puts the errno into its message only
            raise OSError(
                "Can't read data (file read failed, errno = 116, "
                "error message = 'Stale file handle')"
            )
        except OSError:
            log.error("read_failed_h5", exc_info=True)
        events = {e["event"]: e for e in log_events()}
        assert events["read_failed"]["exc_errno"] == 5
        assert events["read_failed_h5"]["exc_errno"] == 116
    # the apostrophe in "Can't" does not start a quote that swallows the message
    text = redact_text(
        "Can't read data (errno = 5, error message = 'Input/output error')", "standard", "s"
    )
    assert (
        text is not None
        and text.startswith("Can't read data (errno = 5")
        and "Input/output error" not in text
    )


def test_every_path_setting_is_a_name_field() -> None:
    """A new setting holding a path must be tokenised at strict (its field name in NAME_FIELDS)."""
    import typing

    from pydantic import BaseModel

    from canopy.config import Config
    from canopy.logs import NAME_FIELDS

    def paths(model: type[BaseModel], prefix: str = "") -> list[str]:
        out = []
        for name, f in model.model_fields.items():
            ann = f.annotation
            args = typing.get_args(ann) or (ann,)
            if any(a is Path for a in args) or "Path" in str(ann):
                out.append(name)
            for a in args:
                if isinstance(a, type) and issubclass(a, BaseModel):
                    out += paths(a, f"{prefix}{name}.")
        return out

    missing = [p for p in paths(Config) if p not in NAME_FIELDS]
    assert missing == []


def test_token_find_names_a_strict_token(cfg: Config, tmp_path: Path) -> None:
    import anndata as ad2
    import numpy as np
    from typer.testing import CliRunner

    from canopy.cli import app
    from canopy.privacy import installation_salt

    ad2.AnnData(X=np.zeros((2, 2), np.float32)).write_h5ad(cfg.data_roots[0].path / "PRIVATE.h5ad")
    conf = tmp_path / "c.toml"
    root = cfg.data_roots[0].path
    conf.write_text(
        f'cache_dir = "{cfg.cache_dir}"\n[[data_roots]]\nname = "data"\npath = "{root}"\n'
    )
    from canopy.catalog import Catalog
    from canopy.config import load_config

    Catalog(load_config(conf)).scan_now()
    t = token("PRIVATE.h5ad", installation_salt(cfg.cache_dir))
    r = CliRunner().invoke(app, ["token", "find", t, "--config", str(conf)])
    assert r.exit_code == 0 and "PRIVATE.h5ad" in r.output
