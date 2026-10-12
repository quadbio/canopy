from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from canopy.build import build_id
from canopy.cli import app
from canopy.logs import log_file_name
from canopy.synth import write_anndata
from canopy.synth.spec import AnnDataSpec, ColumnSpec

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the per-user fallback folder (logs, salt) from the real home."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("CANOPY_CONFIG", raising=False)
    return tmp_path / "xdg" / "canopy"


@pytest.fixture
def h5ad(tmp_path: Path) -> Path:
    spec = AnnDataSpec(
        n_obs=20, n_vars=5, obs=[ColumnSpec(name="patient_secret", kind="categorical")]
    )
    return write_anndata(spec, tmp_path / "project_x.h5ad")


def test_inspect_from_read_only_folder(
    home: Path, h5ad: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ro = tmp_path / "share"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        monkeypatch.chdir(ro)
        r = runner.invoke(app, ["inspect", str(h5ad)])
    finally:
        ro.chmod(0o755)
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["anndata"]["n_obs"] == 20
    assert not (ro / "logs").exists()
    assert (home / "logs" / log_file_name("cli")).is_file()


def test_inspect_strict(home: Path, h5ad: Path) -> None:
    r = runner.invoke(app, ["inspect", str(h5ad), "--level", "STRICT"])
    assert r.exit_code == 0, r.output
    assert "patient_secret" not in r.stdout and "project_x" not in r.stdout
    assert json.loads(r.stdout)["redacted"] is True
    assert (home / ".canopy_salt").is_file()
    # the salt never appears in the output
    assert (home / ".canopy_salt").read_text().strip() not in r.stdout


def test_inspect_rejects_unknown_level(home: Path, h5ad: Path) -> None:
    r = runner.invoke(app, ["inspect", str(h5ad), "--level", "strcit"])
    assert r.exit_code != 0
    assert "patient_secret" not in r.stdout


def test_config_from_environment(home: Path, h5ad: Path, tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    conf = tmp_path / "config.toml"
    conf.write_text(
        'cache_dir = "cache"\n[logging]\ndir = "mylogs"\n'
        '[[data_roots]]\nname = "d"\npath = "data"\n'
    )
    r = runner.invoke(app, ["inspect", str(h5ad)], env={"CANOPY_CONFIG": str(conf)})
    assert r.exit_code == 0, r.output
    assert (tmp_path / "mylogs" / log_file_name("cli")).is_file()


def test_bad_config_is_reported(home: Path, h5ad: Path, tmp_path: Path) -> None:
    conf = tmp_path / "bad.toml"
    conf.write_text("[memmory]\nbudget_gb = 800\n")
    r = runner.invoke(app, ["inspect", str(h5ad), "--config", str(conf)])
    assert r.exit_code == 2
    assert "memmory" in r.output


def test_contract_example_refuses_foreign_folder(home: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    r = runner.invoke(app, ["contract", "example", str(victim)])
    assert r.exit_code == 1 and (victim / "keep.txt").exists()
    assert os.listdir(victim) == ["keep.txt"]


def test_version_shows_build(home: Path) -> None:
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and r.stdout.strip() == f"canopy {build_id()}"


def test_catalog_scan(home: Path, h5ad: Path, tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    h5ad.rename(data / "sub" / h5ad.name)
    conf = tmp_path / "config.toml"
    conf.write_text('cache_dir = "cache"\n[[data_roots]]\nname = "d"\npath = "data"\n')
    r = runner.invoke(app, ["catalog", "scan", "--config", str(conf)])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["new"] == 1
    assert (tmp_path / "cache" / "catalog.sqlite").is_file()
