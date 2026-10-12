from __future__ import annotations

from pathlib import Path

from conftest import log_events
from fastapi.testclient import TestClient

from canopy.app import create_app
from canopy.config import Config


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
    body = r.json()
    assert body["detail"]["code"] == "internal_error" and body["request_id"] == "req-1"
    assert "req-1" in body["detail"]["message"]
    assert r.headers["x-request-id"] == "req-1" and "server-timing" in r.headers
    ev = next(e for e in log_events() if e["event"] == "request_failed")
    assert ev["status"] == 500 and ev["route"] == "/api/boom/{name}" and ev["ms"] >= 0
    assert ev["exc_type"] == "RuntimeError"
    # standard privacy: the quoted name in the message is a token
    assert "patient42" not in ev["exc_message"]


def test_client_telemetry_is_logged_and_bounded(cfg: Config) -> None:
    with TestClient(create_app(cfg, background=False)) as client:
        ok = client.post(
            "/api/telemetry",
            json={
                "session": "s-1",
                "events": [{"name": "first_draw", "ms": 12.5, "fields": {"n_points": 51787}}],
            },
        )
        bad = client.post(
            "/api/telemetry",
            json={"session": "s-1", "events": [{"name": "x", "fields": {"k": "a'name\nwith"}}]},
        )
    assert ok.status_code == 204 and bad.status_code == 422
    ev = next(e for e in log_events() if e["event"] == "client_event")
    assert ev["client_event"] == "first_draw" and ev["c_n_points"] == 51787 and ev["ms"] == 12.5


def test_error_envelope_is_the_same_everywhere(cfg: Config) -> None:
    with TestClient(create_app(cfg, background=False)) as client:
        replies = [
            client.get("/api/catalog/tree", params={"root": "nope"}),  # ApiError
            client.get("/api/stores/0123456789abcdef/c0/x"),  # HTTPException
            client.get("/api/catalog/search"),  # validation error (q missing)
            client.get("/api/nope"),  # no route
        ]
    for r in replies:
        body = r.json()
        assert set(body) == {"detail", "request_id"}, body
        assert set(body["detail"]) == {"code", "message"}
    assert [r.json()["detail"]["code"] for r in replies] == [
        "unknown_root",
        "not_found",
        "invalid_request",
        "not_found",
    ]
    logged = [e for e in log_events() if e["event"] == "request" and e["status"] >= 400]
    assert logged[0]["code"] == "unknown_root"


def test_foreign_host_and_simple_posts_are_refused(cfg: Config) -> None:
    with TestClient(create_app(cfg, background=False)) as client:
        rebinding = client.get("/api/health", headers={"host": "evil.example:8765"})
        form_post = client.post("/api/catalog/refresh", data={"x": "1"})
        json_post = client.post("/api/catalog/refresh", json={})
    assert rebinding.status_code == 400
    assert rebinding.json()["detail"]["code"] == "host_not_allowed"
    assert form_post.status_code == 415
    assert json_post.status_code == 202


def test_request_log_has_dataset_id_and_slow_requests_are_reported(cfg: Config) -> None:
    import threading
    import time

    from canopy.app import InFlight

    inflight = InFlight()
    inflight.start("r1", {"method": "GET", "path_params": {"id": "abc123"}})
    time.sleep(0.05)
    inflight.report_slow(0.01)
    inflight.report_slow(0.01)  # not again within 30 s
    slow = [e for e in log_events() if e["event"] == "slow_request"]
    assert len(slow) == 1 and slow[0]["dataset_id"] == "abc123" and slow[0]["age_s"] >= 0
    inflight.end("r1")
    assert threading.active_count() >= 1


def test_stack_dump_on_signal(tmp_path: Path) -> None:
    import os
    import signal
    import time

    from canopy.logs import install_stack_dumps

    path = install_stack_dumps(tmp_path)
    assert path is not None
    os.kill(os.getpid(), signal.SIGUSR1)
    time.sleep(0.2)
    text = path.read_text()
    assert "Current thread" in text or "Thread" in text
    assert "test_app.py" in text


def test_telemetry_drops_free_text_and_limits_rate(cfg: Config) -> None:
    from canopy import telemetry

    with TestClient(create_app(cfg, background=False)) as client:
        client.post(
            "/api/telemetry",
            json={
                "session": "s-2",
                "events": [{"name": "x", "fields": {"label": "SECRET value", "type": "embedding"}}],
            },
        )
        old = telemetry.RATE_PER_MINUTE
        telemetry.RATE_PER_MINUTE = 3
        try:
            client.post(
                "/api/telemetry",
                json={"session": "s-3", "events": [{"name": "y"}] * 5},
            )
        finally:
            telemetry.RATE_PER_MINUTE = old
    events = log_events()
    x = next(e for e in events if e.get("client_event") == "x")
    assert "c_label" not in x and x["c_type"] == "embedding"
    assert sum(e.get("client_event") == "y" for e in events) == 3
    dropped = [e for e in events if e["event"] == "client_events_dropped"]
    assert {d["client_session"] for d in dropped} == {"s-2", "s-3"}


def test_the_host_config_template_is_a_valid_config() -> None:
    import tomllib
    from pathlib import Path

    from canopy.config import Config

    raw = tomllib.loads((Path(__file__).parents[2] / "config.host.example.toml").read_text())
    cfg = Config.model_validate(raw)
    assert cfg.logging.privacy == "strict" and cfg.server.auth == "token"
    assert cfg.state_dir is not None and cfg.catalog.rescan_minutes == 60
