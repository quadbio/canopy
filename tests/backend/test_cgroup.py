"""cgroup memory limits on Linux hosts: v2, a limit on a parent slice, hybrid and v1 (fake
/proc and /sys trees)."""

from __future__ import annotations

from pathlib import Path

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
