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
