"""Static serving of converted stores (`<cache_dir>/<dataset_version>/c<major>/`) for Viv and
zarrita, with HTTP Range requests (contract SPEC §1.7).

- Single ranges only: `bytes=a-b`, `bytes=a-` and suffix ranges `bytes=-n`; an end past the file
  is clamped; a start at or past the end is 416. Several ranges, or a malformed one, are answered
  with the whole file (allowed by RFC 9110).
- Replies are streamed in 1 MB blocks, so a large range never sits in memory as a whole.
- Paths are relative keys inside one complete store (one with `manifest.json`); hidden or
  `.`/`..` segments are refused and the resolved path must stay inside the store.
- Whole files are revalidated by their ETag (`no-cache`): a forced re-conversion rebuilds a
  store at the same URL. Range replies are not cached by the browser
  (`no-store`) and never answered with 304: browsers that combine cached parts of a shard file
  with many concurrent ranges returned empty bodies (review S6, B-B2). The app keeps its own
  caches of what it decoded.
- Requests are logged in aggregate (`store_requests`, once a minute per store and element
  kind: count, bytes, statuses, time), not one line each; failures and slow replies (> 1 s) are
  also logged one by one.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/stores", tags=["stores"])

VERSION = re.compile(r"[0-9a-f]{16}")
CONTRACT = re.compile(r"c\d{1,3}")
RANGE = re.compile(r"bytes=(\d*)-(\d*)")
BLOCK = 1 << 20
# Whole files: revalidated by ETag (a forced re-conversion rebuilds a store at the same URL; the
# app itself reads with no-store, so this matters for other readers).
CACHE_FOREVER = "no-cache"
NO_STORE = "no-store"
FLUSH_S = 60.0
SLOW_MS = 1000.0


class StoreStats:
    """Store requests summed per (store version, element kind), logged once a minute."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        self._since = time.monotonic()

    def record(self, path: str, status: int, size: int, ms: float, log: Any) -> None:
        parts = path.split("/")  # "", "api", "stores", version, contract, kind, …
        version = parts[3] if len(parts) > 3 else "?"
        kind = parts[5] if len(parts) > 5 else "?"
        kind = kind if kind in ("images", "labels", "shapes", "points", "tables", "aux") else "meta"
        with self._lock:
            row = self._rows.setdefault(
                (version, kind),
                {"n": 0, "bytes": 0, "ms_total": 0.0, "ms_max": 0.0, "status": defaultdict(int)},
            )
            row["n"] += 1
            row["bytes"] += size
            row["ms_total"] += ms
            row["ms_max"] = max(row["ms_max"], ms)
            row["status"][str(status)] += 1
            due = time.monotonic() - self._since >= FLUSH_S
        if due:
            self.flush(log)

    def flush(self, log: Any) -> None:
        with self._lock:
            rows, self._rows = self._rows, {}
            seconds = round(time.monotonic() - self._since, 1)
            self._since = time.monotonic()
        for (version, kind), r in rows.items():
            log.info(
                "store_requests",
                version=version,
                kind=kind,
                seconds=seconds,
                n=r["n"],
                bytes=r["bytes"],
                ms_mean=round(r["ms_total"] / max(r["n"], 1), 2),
                ms_max=round(r["ms_max"], 2),
                status=dict(r["status"]),
            )


stats = StoreStats()


def store_file(cache_dir: Path, version: str, contract: str, key: str) -> Path:
    if not VERSION.fullmatch(version) or not CONTRACT.fullmatch(contract):
        raise HTTPException(404, "unknown store")
    parts = key.split("/")
    if len(key) > 1024 or any(p in ("", ".", "..") for p in parts) or "\\" in key:
        raise HTTPException(400, "invalid key")
    if any(p.startswith(".") for p in parts):
        # Hidden names are never served. 404, not 400: readers probe zarr v2 metadata
        # (`.zattrs`, `.zgroup`) and treat only 404 as "absent".
        raise HTTPException(404, "not found")
    base = os.path.realpath(cache_dir / version / contract)
    if not os.path.isfile(os.path.join(base, "manifest.json")):
        raise HTTPException(404, "unknown store")  # missing or still being written
    real = os.path.realpath(os.path.join(base, *parts))
    if not real.startswith(base + os.sep) or not os.path.isfile(real):
        raise HTTPException(404, "not found")
    return Path(real)


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """(start, end inclusive) of a single byte range, None to send the whole file.
    Raises HTTPException(416) if unsatisfiable."""
    if "," in header:
        return None
    m = RANGE.fullmatch(header.strip())
    if m is None or (m[1] == "" and m[2] == ""):
        return None
    if m[1] == "":  # suffix: the last n bytes
        n = int(m[2])
        if n == 0 or size == 0:
            raise HTTPException(416, headers={"content-range": f"bytes */{size}"})
        return max(0, size - n), size - 1
    start = int(m[1])
    if m[2] and int(m[2]) < start:
        return None  # malformed (last < first): ignore the header
    end = min(int(m[2]), size - 1) if m[2] else size - 1
    if start >= size:
        raise HTTPException(416, headers={"content-range": f"bytes */{size}"})
    return start, end


def read_blocks(path: Path, start: int, end: int) -> Iterator[bytes]:
    """Bytes start..end (inclusive) of a file, in blocks of at most BLOCK bytes."""
    fd = os.open(path, os.O_RDONLY)
    try:
        pos = start
        while pos <= end:
            chunk = os.pread(fd, min(BLOCK, end - pos + 1), pos)
            if not chunk:  # the file shrank while being read
                break
            yield chunk
            pos += len(chunk)
    finally:
        os.close(fd)


@router.api_route("/{version}/{contract}/{key:path}", methods=["GET", "HEAD"])
def store(request: Request, version: str, contract: str, key: str) -> Response:
    path = store_file(request.app.state.config.cache_dir, version, contract, key)
    st = path.stat()
    etag = f'"{st.st_size:x}-{st.st_mtime_ns:x}"'
    headers = {"accept-ranges": "bytes", "etag": etag, "cache-control": CACHE_FOREVER}
    ranged = bool(request.headers.get("range"))
    if ranged:
        headers["cache-control"] = NO_STORE
    elif request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    rng = parse_range(request.headers.get("range", ""), st.st_size)
    media = "application/json" if path.suffix == ".json" else "application/octet-stream"
    status = 200
    start, end = 0, st.st_size - 1
    if rng is not None:
        start, end = rng
        status = 206
        headers["content-range"] = f"bytes {start}-{end}/{st.st_size}"
    headers["content-length"] = str(end - start + 1)
    if request.method == "HEAD" or end < start:
        return Response(status_code=status, headers=headers, media_type=media)
    return StreamingResponse(
        read_blocks(path, start, end), status_code=status, headers=headers, media_type=media
    )
