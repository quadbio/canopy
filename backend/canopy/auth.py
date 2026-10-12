"""Access control: one access token per installation (as in Jupyter).

The server keeps a random token in `<state_dir>/access_token` (only the owner can read it) and
prints a link with it at start. Opening the link stores the token in a cookie (HttpOnly,
SameSite=Strict); from then on every page and API request carries it. Requests without it get
401: the API as JSON, the page as a short explanation. `/api/health` stays open.

Whoever has the token sees every dataset in the data roots, with the rights of the user running
the server: share the link only with people allowed to see all of them. `CANOPY_TOKEN` in the
environment sets the token instead (automated tests). The token is never logged.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlencode

from starlette.requests import Request

TOKEN_FILE = "access_token"  # noqa: S105 (a file name, not a secret)
COOKIE = "canopy_token"
OPEN_PATHS = frozenset({"/api/health"})


def access_token(state_dir: Path) -> str:
    """The installation's token: from `CANOPY_TOKEN`, else the token file (created once)."""
    env = os.environ.get("CANOPY_TOKEN", "").strip()
    if env:
        return env
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / TOKEN_FILE
    try:
        token = path.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)
    return token


def link(host: str, port: int, token: str) -> str:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host  # noqa: S104 (a link, not a bind)
    return f"http://{shown}:{port}/?{urlencode({'token': token})}"


def given(request: Request) -> str | None:
    """The token a request carries: cookie, `Authorization: Bearer`, or `?token=`."""
    c = request.cookies.get(COOKIE)
    if c:
        return c
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.query_params.get("token")


def valid(request: Request, token: str) -> bool:
    g = given(request)
    return bool(g) and secrets.compare_digest(g.encode(), token.encode())  # type: ignore[union-attr]


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Canopy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:15vh auto;padding:0 1rem;
color:#222}code{background:#f2f2f2;padding:0 .3em;border-radius:3px}</style></head>
<body><h1>Canopy</h1>
<p>This server needs its access link. Open the link with <code>?token=…</code> that was printed
when the server started, or print it again on the server with
<code>canopy link --config &lt;your config&gt;</code>.</p></body></html>
"""
