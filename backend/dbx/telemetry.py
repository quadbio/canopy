"""Frontend telemetry: the browser posts timing marks and environment facts (WebGL renderer,
screen size, fps), which are written to the server log as `client_event`, so a session on the
host can be analysed from the log files alone.

Events carry numbers and short codes only (ids, view types), never names or values: the
client sends what the log may contain, and the server bounds it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field, StringConstraints

from dbx.logs import get_logger

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

Code = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:/ ()-]{1,80}$")]
Value = float | int | bool | Code


class ClientEvent(BaseModel):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.]{1,64}$")]
    ms: float | None = None
    fields: dict[Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")], Value] = Field(
        default_factory=dict, max_length=24
    )


class ClientBatch(BaseModel):
    session: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,40}$")]
    events: list[ClientEvent] = Field(max_length=200)


@router.post("", status_code=204)
def telemetry(batch: ClientBatch) -> None:
    log = get_logger("dbx.client")
    for e in batch.events:
        log.info(
            "client_event",
            client_session=batch.session,
            client_event=e.name,
            ms=e.ms,
            **{f"c_{k}": v for k, v in e.fields.items()},
        )
