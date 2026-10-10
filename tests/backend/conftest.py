from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from dbx.config import Config, DataRoot, LoggingConfig
from dbx.logs import current_log_file, setup_logging


def log_events() -> list[dict[str, Any]]:
    """Every event this test process has written to its log file so far."""
    f = current_log_file()
    assert f is not None
    return [json.loads(line) for line in f.read_text().splitlines()]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    data = tmp_path / "data"
    data.mkdir()
    c = Config(
        data_roots=[DataRoot(name="data", path=data)],
        cache_dir=tmp_path / "cache",
        logging=LoggingConfig(dir=tmp_path / "logs", memory_sample_interval=0),
    )
    setup_logging(c.logging, role="test", salt="test-salt", to_console=False)
    return c


# ---------------------------------------------------------------- example datasets
# The lab's example data lives next to the repository (not committed). Tests using it are
# skipped when it is missing, or fail when DBX_REQUIRE_EXAMPLES=1 (so a run that is meant to
# cover them cannot pass silently). SpatialData archives are extracted once into .cache/.
REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO.parent / "example_datasets"
CACHE = REPO / ".cache" / "examples"


def missing_example(what: str) -> None:
    if os.environ.get("DBX_REQUIRE_EXAMPLES") == "1":
        pytest.fail(f"example dataset {what} not available (DBX_REQUIRE_EXAMPLES=1)")
    pytest.skip(f"example dataset {what} not available")


def _example(name: str) -> Path:
    src = EXAMPLES / name
    if src.exists():
        return src
    archive = EXAMPLES / f"{name}.tar.gz"
    if not archive.exists():
        missing_example(name)
    target = CACHE / name
    if not target.exists():
        import tarfile

        CACHE.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            tar.extractall(CACHE, filter="data")
    return target


@pytest.fixture(scope="session")
def example() -> object:
    return _example
