"""cgroup memory limits on Linux hosts: v2, a limit on a parent slice, hybrid and v1 (fake
/proc and /sys trees)."""

from __future__ import annotations

from pathlib import Path

import pytest

from canopy.config import Config
from canopy.logs import cgroup_memory_limit


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_v2_takes_the_smallest_limit_up_to_the_root(tmp_path: Path) -> None:
    proc = tmp_path / "proc_cgroup"
    proc.write_text("0::/user.slice/user-1000.slice/session-3.scope\n")
    sys = tmp_path / "sys"
    _write(sys / "cgroup.controllers", "memory")
    leaf = sys / "user.slice" / "user-1000.slice" / "session-3.scope"
    _write(leaf / "memory.max", "max\n")
    _write(leaf / "memory.current", "1000\n")
    _write(leaf / "memory.events", "low 0\nhigh 0\nmax 2\noom 1\noom_kill 1\n")
    _write(sys / "user.slice" / "user-1000.slice" / "memory.max", "8589934592\n")
    _write(sys / "user.slice" / "memory.max", "17179869184\n")
    cg = cgroup_memory_limit(proc, sys)
    assert cg["version"] == "v2" and cg["limit_bytes"] == 8 * 2**30 and cg["limit_depth"] == 1
    assert cg["usage_bytes"] == 1000 and cg["oom_kills"] == 1


def test_hybrid_and_v1_use_the_v1_memory_controller(tmp_path: Path) -> None:
    proc = tmp_path / "proc_cgroup"
    proc.write_text("12:memory:/user.slice\n11:cpu,cpuacct:/user.slice\n0::/user.slice\n")
    sys = tmp_path / "sys"
    _write(sys / "memory" / "user.slice" / "memory.limit_in_bytes", "4294967296\n")
    _write(sys / "memory" / "user.slice" / "memory.usage_in_bytes", "123\n")
    _write(sys / "memory" / "memory.limit_in_bytes", str(2**63) + "\n")  # unlimited at the root
    _write(sys / "memory" / "user.slice" / "memory.oom_control", "oom_kill_disable 0\noom_kill 3\n")
    cg = cgroup_memory_limit(proc, sys)
    assert cg["version"] == "hybrid" and cg["limit_bytes"] == 4 * 2**30 and cg["oom_kills"] == 3
    proc.write_text("12:memory:/user.slice\n")
    assert cgroup_memory_limit(proc, sys)["version"] == "v1"


def test_no_cgroup_information(tmp_path: Path) -> None:
    assert cgroup_memory_limit(tmp_path / "missing", tmp_path)["version"] is None


def test_headroom_is_read_where_the_limit_is_and_leaves_out_file_cache(tmp_path: Path) -> None:
    """A per-user slice's limit is shared by all its sessions: the room left is the slice's
    limit minus the slice's usage (not this session's), without reclaimable file cache."""
    gib = 2**30
    proc = tmp_path / "proc_cgroup"
    proc.write_text("0::/user.slice/user-1000.slice/session-3.scope\n")
    sys = tmp_path / "sys"
    _write(sys / "cgroup.controllers", "memory")
    slice_ = sys / "user.slice" / "user-1000.slice"
    _write(slice_ / "session-3.scope" / "memory.max", "max\n")
    _write(slice_ / "session-3.scope" / "memory.current", f"{2 * gib}\n")
    _write(slice_ / "memory.max", f"{64 * gib}\n")
    _write(slice_ / "memory.current", f"{60 * gib}\n")
    _write(slice_ / "memory.stat", f"anon {50 * gib}\nfile {10 * gib}\ninactive_file {3 * gib}\n")
    cg = cgroup_memory_limit(proc, sys)
    assert cg["limit_bytes"] == 64 * gib and cg["limit_depth"] == 1
    assert cg["usage_bytes"] == 57 * gib and cg["headroom_bytes"] == 7 * gib
    # v1: total_inactive_file
    proc.write_text("12:memory:/user.slice\n")
    v1 = sys / "memory" / "user.slice"
    _write(v1 / "memory.limit_in_bytes", f"{8 * gib}\n")
    _write(v1 / "memory.usage_in_bytes", f"{8 * gib}\n")
    _write(v1 / "memory.stat", f"cache {5 * gib}\ntotal_inactive_file {4 * gib}\n")
    assert cgroup_memory_limit(proc, sys)["headroom_bytes"] == 4 * gib


def test_a_budget_above_the_limit_is_warned_about_at_start(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A memory budget larger than the process may have (e.g. a per-user cgroup limit): said at
    start, with both numbers, so a bundle shows it."""
    from fastapi.testclient import TestClient

    from canopy import app as app_module

    monkeypatch.setattr(app_module, "memory_total", lambda: 4 * 2**30)
    cfg.memory.budget_gb = 8
    with TestClient(app_module.create_app(cfg, background=False)):
        pass
    assert "memory_budget_above_limit" in caplog.text and "'limit_gb': 4.0" in caplog.text


def test_no_budget_warning_within_the_limit(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Within the limit, with room for the workers' overshoot, nothing is said (review A-L2)."""
    from fastapi.testclient import TestClient

    from canopy import app as app_module

    monkeypatch.setattr(app_module, "memory_total", lambda: 64 * 2**30)
    cfg.memory.budget_gb = 32
    cfg.jobs.max_running, cfg.jobs.memory_gb = 2, 8  # overshoot 2 × 5 = 10 GB: 42 < 57.6
    with TestClient(app_module.create_app(cfg, background=False)):
        pass
    assert "memory_budget_above_limit" not in caplog.text
    cfg.jobs.max_running, cfg.jobs.memory_gb = 3, 16  # 32 + 27 = 59 > 57.6
    with TestClient(app_module.create_app(cfg, background=False)):
        pass
    assert "memory_budget_above_limit" in caplog.text
