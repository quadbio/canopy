"""The browser's telemetry fields and the server's must stay the same lists."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from canopy.app import create_app
from canopy.config import Config
from canopy.logs import log_file_name, setup_logging
from canopy.telemetry import STRING_FIELDS

TS = Path(__file__).parents[2] / "frontend" / "src" / "telemetry" / "telemetry.ts"


def test_frontend_and_server_accept_the_same_string_fields() -> None:
    block = re.search(r"STRING_FIELDS = \[(.*?)\] as const", TS.read_text(), re.S)
    assert block is not None
    frontend = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert frontend == set(STRING_FIELDS)


def test_strict_drops_message_text_but_keeps_codes(cfg: Config) -> None:
    cfg.logging.privacy = "strict"
    setup_logging(cfg.logging, role="test", salt="s", to_console=False)
    with TestClient(create_app(cfg, background=False)) as c:
        r = c.post(
            "/api/telemetry",
            json={
                "session": "s1",
                "events": [
                    {
                        "name": "deck_error",
                        "fields": {
                            "type": "Error",
                            "layer": "PathLayer",
                            "message": "SECRETWORD failed",
                            "where": "index-abc.js:1:2",
                            "dataset": "0123456789abcdef",
                        },
                    }
                ],
            },
        )
        assert r.status_code == 204
    text = (cfg.logging.dir / log_file_name("test")).read_text()
    assert "SECRETWORD" not in text and "0123456789abcdef" not in text
    assert "PathLayer" in text and "index-abc.js:1:2" in text


def test_strict_keeps_a_library_message_and_the_caller(cfg: Config) -> None:
    """A library's fixed message (zarrita's, host test 3) says what failed; it stays, also in the
    bundle's re-filtering. Anything else in `message` is still dropped."""
    from canopy.bundle import _strictify

    cfg.logging.privacy = "strict"
    setup_logging(cfg.logging, role="test", salt="s", to_console=False)
    msg = "Input contains an empty iterator."
    with TestClient(create_app(cfg, background=False)) as c:
        ev = {"type": "Error", "message": msg, "where": "a.js:1:2", "caller": "a.js:3:4 a.js:5:6"}
        r = c.post(
            "/api/telemetry", json={"session": "s1", "events": [{"name": "js_error", "fields": ev}]}
        )
        assert r.status_code == 204
    text = (cfg.logging.dir / log_file_name("test")).read_text()
    assert msg in text and "a.js:3:4 a.js:5:6" in text
    assert _strictify(msg, "salt", "c_message") == msg
    assert _strictify("SECRETWORD failed", "salt", "c_message") == "(dropped)"
