"""Access control: the token link, the cookie, refusals; private files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canopy.app import create_app
from canopy.auth import TOKEN_FILE, access_token, cookie_name
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
            f"{cookie_name('tok-123')}=tok-123" in cookie
            and "HttpOnly" in cookie
            and "SameSite=strict" in cookie
        )
        # the cookie now opens the API
        assert c.get("/api/catalog/roots").status_code == 200
        c.cookies.clear()
        assert c.get("/api/catalog/roots").status_code == 401


def test_a_stale_cookie_does_not_block_a_valid_link(
    secured: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Another server's cookie (same address, other port) or an old one: the right token in
    the link or header still opens, and the link re-sets this server's cookie."""
    monkeypatch.setenv("CANOPY_TOKEN", "new-token")
    with TestClient(create_app(secured, background=False)) as c:
        c.cookies.set(cookie_name("new-token"), "old-token")
        c.cookies.set(cookie_name("other-server"), "other-server")
        assert c.get("/api/catalog/roots").status_code == 401
        assert "auth_refused" in caplog.text and "old-token" not in caplog.text
        ok = {"authorization": "Bearer new-token"}
        assert c.get("/api/catalog/roots", headers=ok).status_code == 200
        r = c.get("/?token=new-token", follow_redirects=False)
        assert r.status_code == 303 and "new-token" in r.headers["set-cookie"]
        assert cookie_name("new-token") != cookie_name("other-server")


def test_the_link_never_redirects_to_another_site() -> None:
    """A request for "//other.site/x?token=…" is sent back to a path here, not to that site."""
    from starlette.requests import Request
    from starlette.responses import Response

    from canopy.app import _remember_token

    scope = {
        "type": "http",
        "method": "GET",
        "path": "//evil.example/x",
        "headers": [],
        "query_string": b"token=tok&a=1",
    }
    r = _remember_token(Request(scope), Response(), "canopy_token_x", "tok")
    assert r.headers["location"] == "/evil.example/x?a=1"


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
