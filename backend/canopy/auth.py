"""Access control: two access tokens per installation (as in Jupyter, plus a member link).

The **admin** token (`access_token`) allows everything; the **member** token (`member_token`)
allows everything but cancelling a conversion (owner, full review 2: one conversion of a dataset
runs at a time and everyone sees it, so members may start them). Each request is logged with
which kind of link it came with (`access`), never the token. `canopy token rotate --member` (or
`--admin`) replaces a token, which revokes its old link; the server reads the token files again
within `RELOAD_S` seconds.

The server keeps a random token in `<state_dir>/access_token` (only the owner can read it) and
prints a link with it at start. Opening the link stores the token in a cookie (HttpOnly,
SameSite=Strict) named after the token (`cookie_name`): browsers share cookies between ports, so
two servers on one address keep their own, and an old cookie (a replaced token) is simply not
used. From then on every page and API request carries it; a request is let in when any token it
carries (cookie, Bearer header, `?token=`) is right. Requests without it get
401: the API as JSON, the page as a short explanation. `/api/health` stays open.

Whoever has the token sees every dataset in the data roots, with the rights of the user running
the server: share the link only with people allowed to see all of them. `CANOPY_TOKEN` in the
environment sets the token instead (automated tests). The token is never logged.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from starlette.requests import Request

TOKEN_FILE = "access_token"  # noqa: S105 (a file name, not a secret)
MEMBER_FILE = "member_token"  # noqa: S105
RELOAD_S = 5.0
COOKIE = "canopy_token"  # prefix of the cookie's name
OPEN_PATHS = frozenset({"/api/health"})


def access_token(state_dir: Path) -> str:
    """The admin token: from `CANOPY_TOKEN`, else the token file (created once)."""
    return _token(state_dir / TOKEN_FILE, "CANOPY_TOKEN")


def member_token(state_dir: Path) -> str:
    """The member token: from `CANOPY_MEMBER_TOKEN`, else its file (created once)."""
    return _token(state_dir / MEMBER_FILE, "CANOPY_MEMBER_TOKEN")


def rotate(state_dir: Path, which: str) -> str:
    """A new token for `which` ("admin" or "member"): the old link stops working."""
    path = state_dir / (TOKEN_FILE if which == "admin" else MEMBER_FILE)
    path.unlink(missing_ok=True)
    return _token(path, "")


def _token(path: Path, env_name: str) -> str:
    env = os.environ.get(env_name, "").strip() if env_name else ""
    if env:
        return env
    path.parent.mkdir(parents=True, exist_ok=True)
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


class Tokens:
    """The installation's two tokens, read again every `RELOAD_S` (so a rotation needs no
    restart)."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self._at = -RELOAD_S
        self._pair = ("", "")

    def get(self) -> tuple[str, str]:
        now = time.monotonic()
        if now - self._at >= RELOAD_S:
            self._pair = (access_token(self.state_dir), member_token(self.state_dir))
            self._at = now
        return self._pair


def access_of(request: Request, admin: str, member: str) -> str | None:
    """ "admin" or "member" for the token the request carries (cookie, bearer or query), else
    None. The cookie is named after the admin token (`cookie_name`), whichever it holds."""
    got = carried(request, admin).values()
    for kind, want in (("admin", admin), ("member", member)):
        if any(secrets.compare_digest(g.encode(), want.encode()) for g in got):
            return kind
    return None


def link(host: str, port: int, token: str) -> str:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host  # noqa: S104 (a link, not a bind)
    return f"http://{shown}:{port}/?{urlencode({'token': token})}"


def cookie_name(token: str) -> str:
    """The cookie of this installation: its name carries a short hash of the token."""
    return f"{COOKIE}_{hashlib.sha256(token.encode()).hexdigest()[:8]}"


def carried(request: Request, token: str) -> dict[str, str]:
    """The tokens a request carries, by where: cookie, bearer (`Authorization`), query."""
    out = {}
    if c := request.cookies.get(cookie_name(token)):
        out["cookie"] = c
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer ") and h[7:].strip():
        out["bearer"] = h[7:].strip()
    if q := request.query_params.get("token"):
        out["query"] = q
    return out


def valid(request: Request, token: str) -> bool:
    """Whether any token the request carries is the right one."""
    want = token.encode()
    return any(secrets.compare_digest(g.encode(), want) for g in carried(request, token).values())


def query_token(request: Request) -> str | None:
    return request.query_params.get("token")


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
