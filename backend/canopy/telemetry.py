"""Frontend telemetry: the browser posts timing marks and environment facts (WebGL renderer,
screen size, frame rates), which are written to the server log as `client_event`, so a session
on the host can be analysed from the log files alone.

Events carry numbers and short codes only, never names or values. The server enforces it:
- string values are accepted only for fields in `CODE_FIELDS` (codes the frontend defines, error
  classes, code locations in the app bundle, GPU and browser facts) and `TEXT_FIELDS` (short
  messages, already stripped of quoted parts and parenthesised ids by the browser);
- at `strict` privacy the text fields are dropped (only their presence is kept): a message can
  hold a name; the code location (`where`) and the build still identify the failure. `code` and
  `kind` are kept when they are the program's own (`too_many_values`,
  `needs_conversion:legacy_layout`, `gene`), and `message` when it is a library's fixed text
  (`LIBRARY_MESSAGES`); see `plain_value`;
- any other string is dropped and counted.
Rates: `RATE_PER_MINUTE` per browser session, `ADDRESS_PER_MINUTE` per client address and
`GLOBAL_PER_MINUTE` in all, so one client cannot flood the log; the rest are dropped and counted
(one warning per batch).
"""

from __future__ import annotations

import threading
import time
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, StringConstraints

from canopy.errors import known_code
from canopy.logs import get_logger

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

Code = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:/ ()-]{1,80}$")]
Value = float | int | bool | Code
CODE_FIELDS = frozenset(
    {
        "type",  # error class, view type
        "mode",
        "status",
        "layer",  # deck.gl layer class
        "where",  # bundle file:line:col of an error
        "caller",  # the next two frames of its stack (`where` is often inside a library)
        "build",  # frontend build id
        "browser",  # browser family and major version
        "view",  # the view's random id
        "dataset",  # catalog id (a hash; tokenised at strict as c_dataset)
        "fit",
        "x_source",  # "X" or "layer"
        "gpu",
        "gpu_vendor",
        "gl_version",
    }
)
TEXT_FIELDS = frozenset({"message", "code", "kind"})
# At strict, `code` and `kind` survive only when they are the program's own: a refusal code
# (errors.known_code) or one of these kinds (dataset kinds, colourings, plot summaries). Any
# other value (e.g. a name sent by mistake) is dropped (review A-M2).
CLIENT_KINDS = frozenset(
    {"h5ad", "anndata_zarr", "spatialdata_zarr", "obs", "gene", "none", "violin", "bars", "density"}
)
# Messages of the libraries in the frontend bundle, as the browser reduces them (`messageCode`):
# fixed text, kept at strict (a message is otherwise dropped, as it can hold a name).
LIBRARY_MESSAGES = frozenset(
    {
        "Input contains an empty iterator.",  # zarrita: a read of an empty array or slice
        "slice step cannot be zero",
        "The operation was aborted.",
        "signal is aborted without reason",
        "Failed to fetch",
        "Load failed",
        "NetworkError when attempting to fetch resource.",
    }
)
CODE_LIKE_FIELDS = frozenset({"code", "kind", "message"})


def plain_value(key: str, v: str) -> bool:
    """Whether a `code`, `kind` or `message` value is the program's or a library's own fixed
    text (kept at strict)."""
    if key == "code":
        return known_code(v)
    if key == "kind":
        return v in CLIENT_KINDS
    return key == "message" and v in LIBRARY_MESSAGES


STRING_FIELDS = CODE_FIELDS | TEXT_FIELDS  # the frontend's list must match (test)
RATE_PER_MINUTE = 600
ADDRESS_PER_MINUTE = 1_500
GLOBAL_PER_MINUTE = 3_000


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

    def allow(self, session: str, n: int, per_minute: int | None = None) -> int:
        """How many of `n` events this key may still log in the current minute."""
        minute = int(time.time() // 60)
        with self._lock:
            if len(self._window) > 10_000:  # forget old sessions
                self._window = {k: v for k, v in self._window.items() if v[0] == minute}
            m, count = self._window.get(session, (minute, 0))
            if m != minute:
                count = 0
            cap = RATE_PER_MINUTE if per_minute is None else per_minute
            ok = max(0, min(n, cap - count))
            self._window[session] = (minute, count + ok)
            return ok


_limit = _RateLimit()
_by_address = _RateLimit()
_overall = _RateLimit()


@router.post("", status_code=204)
def telemetry(batch: ClientBatch, request: Request) -> None:
    log = get_logger("canopy.client")
    strict = request.app.state.config.logging.privacy == "strict"
    address = request.client.host if request.client else "?"
    allowed = min(
        _limit.allow(batch.session, len(batch.events)),
        _by_address.allow(address, len(batch.events), ADDRESS_PER_MINUTE),
        _overall.allow("*", len(batch.events), GLOBAL_PER_MINUTE),
    )
    dropped_fields = 0
    for e in batch.events[:allowed]:
        fields: dict[str, object] = {}
        for k, v in e.fields.items():
            if isinstance(v, str) and k not in STRING_FIELDS:
                dropped_fields += 1
                continue
            plain = k in CODE_LIKE_FIELDS and isinstance(v, str) and plain_value(k, v)
            if isinstance(v, str) and strict and k in TEXT_FIELDS and not plain:
                fields[f"c_{k}"] = "(text dropped at strict)"
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
