"""Build identity (version + git commit) for logs, `/api/version` and `canopy --version`.

A packaged install reads `_build.json`, written at packaging time (S9). A source checkout reads
the commit from `.git` directly (no subprocess). Uncommitted changes are not detected.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import canopy

BUILD_FILE = Path(__file__).with_name("_build.json")
REPO = Path(__file__).resolve().parents[2]


def _git_commit(git: Path) -> str | None:
    try:
        head = (git / "HEAD").read_text().strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return head or None
    ref = head[5:]
    try:
        return (git / ref).read_text().strip()
    except OSError:
        pass
    try:
        for line in (git / "packed-refs").read_text().splitlines():
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha
    except OSError:
        pass
    return None


@cache
def build_info() -> dict[str, str | None]:
    if BUILD_FILE.is_file():
        data = json.loads(BUILD_FILE.read_text())
        return {"version": canopy.__version__, "commit": data.get("commit"), "source": "package"}
    return {"version": canopy.__version__, "commit": _git_commit(REPO / ".git"), "source": "git"}


def build_id() -> str:
    """`<version>+g<commit10>`, or the version alone when the commit is unknown."""
    info = build_info()
    commit = info["commit"]
    return f"{info['version']}+g{commit[:10]}" if commit else str(info["version"])
