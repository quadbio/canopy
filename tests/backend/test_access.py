"""Two links: admin (everything) and member (everything but cancelling conversions); which one
a request came with is logged, never the token (owner, full review 2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import canopy.cli as cli_mod
from canopy.app import create_app
from canopy.auth import RELOAD_S, access_token, member_token
from canopy.cli import app
from canopy.config import Config
from canopy.logs import log_file_name, setup_logging


@pytest.fixture
def secured(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.delenv("CANOPY_TOKEN", raising=False)
    monkeypatch.delenv("CANOPY_MEMBER_TOKEN", raising=False)
    cfg.server.auth = "token"
    return cfg


def test_member_link_may_not_cancel_admin_may(secured: Config) -> None:
    cfg = secured
    setup_logging(cfg.logging, role="test", salt="s", to_console=False)
    admin, member = access_token(cfg.state), member_token(cfg.state)
    assert admin != member
    with TestClient(create_app(cfg, background=False)) as c:
        assert c.get("/api/me").status_code == 401
        m = {"authorization": f"Bearer {member}"}
        a = {"authorization": f"Bearer {admin}"}
        assert c.get("/api/me", headers=m).json() == {"access": "member"}
        assert c.get("/api/me", headers=a).json() == {"access": "admin"}
        r = c.post("/api/jobs/1/cancel", json={}, headers=m)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "admin_only"
        assert c.post("/api/jobs/1/cancel", json={}, headers=a).status_code == 404  # no such job
        # the member's link becomes a member cookie
        r = c.get(f"/api/me?token={member}")
        assert r.json() == {"access": "member"}
        assert c.get("/api/me").json() == {"access": "member"}
    text = (cfg.logging.dir / log_file_name("test")).read_text()
    assert '"access": "member"' in text and '"access": "admin"' in text
    assert member not in text and admin not in text


def test_a_new_member_link_revokes_the_old(
    secured: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = secured
    old = member_token(cfg.state)
    monkeypatch.setattr(cli_mod, "_load", lambda config, **kw: cfg)
    conf = tmp_path / "c.toml"
    conf.touch()
    with TestClient(create_app(cfg, background=False)) as c:
        h = {"authorization": f"Bearer {old}"}
        assert c.get("/api/me", headers=h).status_code == 200
        r = CliRunner().invoke(app, ["link", "-c", str(conf), "--member", "--new"])
        assert r.exit_code == 0 and old not in r.output and "token=" in r.output
        monkeypatch.setattr("canopy.auth.time.monotonic", lambda: 10**9)  # past RELOAD_S
        assert RELOAD_S > 0
        assert c.get("/api/me", headers=h).status_code == 401
        new = member_token(cfg.state)
        assert c.get("/api/me", headers={"authorization": f"Bearer {new}"}).json() == {
            "access": "member"
        }


def test_one_dataset_cannot_take_every_worker() -> None:
    """Beyond PER_DATASET requests waiting for one dataset the next is refused (A-M6)."""
    from canopy.app import PER_DATASET, InFlight

    f = InFlight()
    scope = {"path": "/api/datasets/abc/gene"}
    assert all(f.start(f"r{i}", scope) for i in range(PER_DATASET))
    assert not f.start("one-more", scope)
    assert f.start("other", {"path": "/api/datasets/xyz/gene"})  # other datasets still go
    f.end("r0")
    assert f.start("again", scope)
