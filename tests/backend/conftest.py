from __future__ import annotations

from pathlib import Path

import pytest

from dbx.config import Config, DataRoot, LoggingConfig
from dbx.logs import setup_logging


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    data = tmp_path / "data"
    data.mkdir()
    c = Config(
        data_roots=[DataRoot(name="data", path=data)],
        cache_dir=tmp_path / "cache",
        logging=LoggingConfig(dir=tmp_path / "logs", memory_sample_interval=0),
    )
    setup_logging(c.logging, to_console=False)
    return c


# ---------------------------------------------------------------- example datasets
# The lab's example data lives next to the repository (not committed). Tests using it are
# skipped when it is missing; SpatialData archives are extracted once into .cache/.
REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO.parent / "example_datasets"
CACHE = REPO / ".cache" / "examples"


def _example(name: str) -> Path:
    src = EXAMPLES / name
    if src.exists():
        return src
    archive = EXAMPLES / f"{name}.tar.gz"
    if not archive.exists():
        pytest.skip(f"example dataset {name} not available")
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
