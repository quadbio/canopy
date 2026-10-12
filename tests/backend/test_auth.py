"""Access control: the token link, the cookie, refusals; private files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canopy.app import create_app
from canopy.auth import COOKIE, TOKEN_FILE, access_token
from canopy.config import Config, apply_file_access


@pytest.fixture
def secured(cfg: Config) -> Config:
    cfg.server.auth = "token"
    return cfg


def test_token_is_created_once_and_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANOPY_TOKEN", raising=False)
    t = access_token(tmp_path)
    assert len(t) >= 40 and access_token(tmp_path) == t
    assert stat.S_IMODE((tmp_path / TOKEN_FILE).stat().st_mode) == 0o600
    monkeypatch.setenv("CANOPY_TOKEN", "from-env")
    assert access_token(tmp_path) == "from-env"


def test_requests_without_the_token_are_refused(
    secured: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CANOPY_TOKEN", raising=False)
    with TestClient(create_app(secured, background=False)) as c:
        token = access_token(secured.state)
        assert c.get("/api/health").status_code == 200  # open
        r = c.get("/api/catalog/roots")
        assert r.status_code == 401 and r.json()["detail"]["code"] == "auth_required"
        assert c.get("/").status_code == 401
        assert (
            c.get("/api/catalog/roots", headers={"authorization": "Bearer wrong"}).status_code
            == 401
        )
        assert (
            c.get("/api/catalog/roots", headers={"authorization": f"Bearer {token}"}).status_code
            == 200
        )
        assert c.post("/api/jobs/1/cancel", json={}).status_code == 401
        assert c.get("/docs").status_code in (401, 404) and c.get("/openapi.json").status_code in (
            401,
            404,
        )


def test_the_link_sets_a_cookie_and_drops_the_token_from_the_address(
    secured: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CANOPY_TOKEN", "tok-123")
    with TestClient(create_app(secured, background=False)) as c:
        r = c.get("/?token=tok-123&view=x", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?view=x"
        cookie = r.headers["set-cookie"]
        assert (
            f"{COOKIE}=tok-123" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        )
        # the cookie now opens the API
        assert c.get("/api/catalog/roots").status_code == 200
        c.cookies.clear()
        assert c.get("/api/catalog/roots").status_code == 401


def test_files_are_private_or_group_readable(cfg: Config, tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        apply_file_access(cfg)
        p = tmp_path / "private.txt"
        p.write_text("x")
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        cfg.server.file_access = "group"
        apply_file_access(cfg)
        q = tmp_path / "group.txt"
        q.write_text("x")
        assert stat.S_IMODE(q.stat().st_mode) == 0o640
    finally:
        os.umask(old)
