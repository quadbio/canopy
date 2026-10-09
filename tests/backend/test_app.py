from __future__ import annotations

import json

from fastapi.testclient import TestClient

from dbx.app import create_app
from dbx.config import Config
from dbx.logs import LOG_FILE


def _events(cfg: Config) -> list[dict[str, object]]:
    lines = (cfg.logging.dir / LOG_FILE).read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_health_and_request_log(cfg: Config) -> None:
    with TestClient(create_app(cfg)) as client:
        r = client.get("/api/health", headers={"x-request-id": "abc123"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.headers["x-request-id"] == "abc123"
    assert "server-timing" in r.headers

    events = _events(cfg)
    names = [e["event"] for e in events]
    assert "startup" in names and "shutdown" in names
    req = next(e for e in events if e["event"] == "request")
    assert req["request_id"] == "abc123"
    assert req["path"] == "/api/health"
    assert req["status"] == 200
    assert isinstance(req["ms"], float)


def test_version_reports_packages(cfg: Config) -> None:
    with TestClient(create_app(cfg)) as client:
        body = client.get("/api/version").json()
    assert body["packages"]["anndata"]
    assert body["python"].startswith("3.")
