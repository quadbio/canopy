from __future__ import annotations

import json

import pytest

from dbx.config import Config
from dbx.logs import LOG_FILE, memory_snapshot, timed


def test_timed_logs_stage_and_extra_fields(cfg: Config) -> None:
    with timed("read_x", dataset="d1") as extra:
        extra["bytes"] = 123
    ev = json.loads((cfg.logging.dir / LOG_FILE).read_text().splitlines()[-1])
    assert ev["event"] == "stage"
    assert ev["stage"] == "read_x"
    assert ev["dataset"] == "d1"
    assert ev["bytes"] == 123
    assert ev["ms"] >= 0


def test_timed_logs_failure_and_reraises(cfg: Config) -> None:
    with pytest.raises(ValueError), timed("boom"):
        raise ValueError("x")
    ev = json.loads((cfg.logging.dir / LOG_FILE).read_text().splitlines()[-1])
    assert ev["event"] == "stage_failed"
    assert "ValueError" in ev["exception"]


def test_memory_snapshot_fields(cfg: Config) -> None:
    snap = memory_snapshot()
    assert snap["rss_mb"] > 0
    assert snap["host_total_gb"] > 0
