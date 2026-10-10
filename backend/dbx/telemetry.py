"""Frontend telemetry: the browser posts timing marks and environment facts (WebGL renderer,
screen size, frame rates), which are written to the server log as `client_event`, so a session
on the host can be analysed from the log files alone.

Events carry numbers and short codes only, never names or values. The server enforces it:
string values are accepted only for fields on `STRING_FIELDS` (codes the frontend defines, plus
GPU and browser facts); any other string is dropped and counted. Each browser session may send
at most `RATE_PER_MINUTE` events per minute; the rest are dropped and counted.
"""

from __future__ import annotations

import threading
import time
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field, StringConstraints

from dbx.logs import get_logger

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

Code = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:/ ()-]{1,80}$")]
Value = float | int | bool | Code
STRING_FIELDS = frozenset(
    {"type", "kind", "mode", "status", "code", "gpu", "gpu_vendor", "gl_version", "browser"}
)
RATE_PER_MINUTE = 600


class ClientEvent(BaseModel):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.]{1,64}$")]
    ms: float | None = None
    fields: dict[Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")], Value] = Field(
        default_factory=dict, max_length=24
    )


class ClientBatch(BaseModel):
    session: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,40}$")]
    events: list[ClientEvent] = Field(max_length=200)


class _RateLimit:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window: dict[str, tuple[int, int]] = {}  # session -> (minute, count)

    def allow(self, session: str, n: int) -> int:
        """How many of `n` events this session may still log in the current minute."""
        minute = int(time.time() // 60)
        with self._lock:
            if len(self._window) > 10_000:  # forget old sessions
                self._window = {k: v for k, v in self._window.items() if v[0] == minute}
            m, count = self._window.get(session, (minute, 0))
            if m != minute:
                count = 0
            ok = max(0, min(n, RATE_PER_MINUTE - count))
            self._window[session] = (minute, count + ok)
            return ok


_limit = _RateLimit()


@router.post("", status_code=204)
def telemetry(batch: ClientBatch) -> None:
    log = get_logger("dbx.client")
    allowed = _limit.allow(batch.session, len(batch.events))
    dropped_fields = 0
    for e in batch.events[:allowed]:
        fields = {}
        for k, v in e.fields.items():
            if isinstance(v, str) and k not in STRING_FIELDS:
                dropped_fields += 1
                continue
            fields[f"c_{k}"] = v
        log.info(
            "client_event", client_session=batch.session, client_event=e.name, ms=e.ms, **fields
        )
    if allowed < len(batch.events) or dropped_fields:
        log.warning(
            "client_events_dropped",
            client_session=batch.session,
            over_rate=len(batch.events) - allowed,
            string_fields=dropped_fields,
        )
