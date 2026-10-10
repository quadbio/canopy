from __future__ import annotations

from conftest import log_events
from fastapi.testclient import TestClient

from dbx.app import create_app
from dbx.config import Config


def test_health_and_request_log(cfg: Config) -> None:
    with TestClient(create_app(cfg, background=False)) as client:
        r = client.get("/api/health", headers={"x-request-id": "abc123"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.headers["x-request-id"] == "abc123"
    assert "server-timing" in r.headers

    events = log_events()
    names = [e["event"] for e in events]
    assert "startup" in names and "shutdown" in names
    req = next(e for e in events if e["event"] == "request")
    assert req["request_id"] == "abc123"
    assert req["path"] == "/api/health"
    assert req["status"] == 200
    assert isinstance(req["ms"], float)


def test_version_reports_packages(cfg: Config) -> None:
    with TestClient(create_app(cfg, background=False)) as client:
        body = client.get("/api/version").json()
    assert body["packages"]["anndata"]
    assert body["python"].startswith("3.")
    assert body["hdf5"] and body["memory_available_gb"] > 0
    assert body["hdf5_file_locking"] == "FALSE"


def test_startup_logs_filesystems_and_build(cfg: Config) -> None:
    with TestClient(create_app(cfg, background=False)):
        pass
    start = next(e for e in log_events() if e["event"] == "startup")
    assert start["cache_fs"]["fstype"]
    assert [r["root"] for r in start["root_fs"]] == ["data"]
    assert start["build"] and start["role"] == "test" and start["pid"]


def test_request_id_is_bounded(cfg: Config) -> None:
    with TestClient(create_app(cfg, background=False)) as client:
        r = client.get("/api/health", headers={"x-request-id": "x" * 65})
        r2 = client.get("/api/health", headers={"x-request-id": "a b\nc"})
    assert r.headers["x-request-id"] != "x" * 65
    assert len(r.headers["x-request-id"]) == 12
    assert r2.headers["x-request-id"] != "a b\nc"


def test_failed_request_gets_status_id_and_log(cfg: Config) -> None:
    app = create_app(cfg, background=False)

    @app.get("/api/boom/{name}")
    def boom(name: str) -> None:
        raise RuntimeError(f"cannot open '{name}'")

    app.router.routes.insert(0, app.router.routes.pop())  # before the frontend mount at "/"
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/boom/patient42", headers={"x-request-id": "req-1"})
    assert r.status_code == 500
    assert r.json() == {"detail": "internal error", "request_id": "req-1"}
    assert r.headers["x-request-id"] == "req-1" and "server-timing" in r.headers
    ev = next(e for e in log_events() if e["event"] == "request_failed")
    assert ev["status"] == 500 and ev["route"] == "/api/boom/{name}" and ev["ms"] >= 0
    assert ev["exc_type"] == "RuntimeError"
    # standard privacy: the quoted name in the message is a token
    assert "patient42" not in ev["exc_message"]
