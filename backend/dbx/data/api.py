"""Dataset API: metadata as JSON, arrays as binary.

Binary replies are little-endian typed arrays (`application/octet-stream`) described by headers:
`x-dbx-dtype` (numpy name: float32, int8, int16, int32, uint8), `x-dbx-shape` (comma-separated)
and, for obs columns, `x-dbx-kind`. They carry an ETag (dataset version + request), so browsers
revalidate with a 304 instead of downloading again.

Errors are JSON `{"detail": {"code": ..., "message": ...}}`: 404 unknown dataset/column,
409 not previewable (code says why, e.g. `needs_conversion:legacy_layout`), 410 vanished,
403 path left its data root.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from dbx.catalog import Catalog, PathEscapeError
from dbx.catalog.db import Row
from dbx.data.cache import DatasetCache, table_info
from dbx.data.reader import (
    ColumnMeta,
    NeedsConversionError,
    ObsValues,
    Source,
    SourceClosedError,
    embedding_dims,
)
from dbx.inspect.core import version_stamp
from dbx.inspect.models import Fingerprint

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


class Embedding(BaseModel):
    key: str
    dims: int


class DatasetMeta(BaseModel):
    id: str
    version: str
    name: str
    kind: str
    table: str | None
    tables: list[str]
    n_obs: int
    n_vars: int
    mode: str  # lazy | memory
    obs: list[ColumnMeta]
    embeddings: list[Embedding]
    warnings: list[str]


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _state(request: Request) -> tuple[Catalog, DatasetCache]:
    return request.app.state.catalog, request.app.state.datasets


def _dataset(request: Request, id: str) -> tuple[Row, Fingerprint, Path]:
    catalog, _ = _state(request)
    row = catalog.get(id)
    if row is None:
        raise _fail(404, "unknown_dataset", "No dataset with this id in the catalog.")
    if row.state != "inspected" or not row.fingerprint or not row.version:
        raise _fail(409, f"state:{row.state}", f"The dataset is {row.state} ({row.reason}).")
    try:
        path = catalog.resolve(row)
    except PathEscapeError as e:
        raise _fail(403, "path_escape", "The dataset path leaves its data root.") from e
    except FileNotFoundError as e:
        catalog.refresh()
        raise _fail(
            410, "vanished", "The dataset no longer exists; the catalog is rescanning."
        ) from e
    return row, Fingerprint.model_validate_json(row.fingerprint), path


def _with_source[T](
    request: Request, id: str, table: str | None, fn: Callable[[Source, str], T]
) -> T:
    """Run `fn(source, version)` on the dataset's open source (opened on first use)."""
    row, fp, path = _dataset(request, id)
    catalog, cache = _state(request)
    version = str(row.version)
    if not cache.has_source(version, table):
        _check_current(catalog, row, path)
    for attempt in (0, 1):
        try:
            src = cache.source(version, fp, path, table)
            return fn(src, version)
        except SourceClosedError:
            if attempt:
                raise
        except NeedsConversionError as e:
            raise _fail(
                409, f"needs_conversion:{e.code}", "Convert the dataset to view this."
            ) from e
        except KeyError as e:
            raise _fail(404, "unknown_element", "No such table, column or embedding.") from e
        except FileNotFoundError as e:
            catalog.refresh()
            raise _fail(410, "vanished", "The dataset no longer exists.") from e
    raise AssertionError("unreachable")


def _check_current(catalog: Catalog, row: Row, path: Path) -> None:
    """Rows describe the file as it was at the last scan; ask for a rescan if it changed (the
    request is still served from the file as it is now)."""
    try:
        if version_stamp(path) != row.stamp:
            catalog.refresh()
    except OSError:
        pass


def _binary(request: Request, arr: np.ndarray, etag_parts: list[str], **headers: str) -> Response:
    etag = '"' + hashlib.sha256("|".join(etag_parts).encode()).hexdigest()[:20] + '"'
    common = {"etag": etag, "cache-control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=common)
    arr = np.ascontiguousarray(arr)
    if arr.dtype.byteorder == ">":
        arr = arr.astype(arr.dtype.newbyteorder("<"))
    return Response(
        content=arr.tobytes(),
        media_type="application/octet-stream",
        headers={
            **common,
            "x-dbx-dtype": arr.dtype.name,
            "x-dbx-shape": ",".join(str(n) for n in arr.shape),
            **{k.replace("_", "-"): v for k, v in headers.items()},
        },
    )


TableQ = Annotated[str | None, Query(max_length=512)]


@router.get("/{id}/meta")
def meta(request: Request, id: str, table: TableQ = None) -> DatasetMeta:
    row, fp, _ = _dataset(request, id)
    try:
        name, info = table_info(fp, table)
    except NeedsConversionError as e:
        raise _fail(409, f"needs_conversion:{e.code}", "This dataset has no table.") from e
    except KeyError as e:
        raise _fail(404, "unknown_element", "No such table.") from e

    def describe(src: Source, _: str) -> tuple[str, list[ColumnMeta]]:
        return src.mode, [src.column_meta(c.name) for c in info.obs.columns]

    mode, columns = _with_source(request, id, table, describe)
    return DatasetMeta(
        id=row.id,
        version=str(row.version),
        name=row.name,
        kind=fp.kind,
        table=name,
        tables=list(fp.spatialdata.tables) if fp.spatialdata else [],
        n_obs=info.n_obs,
        n_vars=info.n_vars,
        mode=mode,
        obs=columns,
        embeddings=[Embedding(key=k, dims=d) for k, d in embedding_dims(info).items()],
        warnings=sorted({w.code for w in fp.warnings}),
    )


@router.get("/{id}/obs")
def obs(
    request: Request, id: str, column: Annotated[str, Query(max_length=512)], table: TableQ = None
) -> Response:
    _, cache = _state(request)

    def load(src: Source, version: str) -> tuple[ObsValues, list[str]]:
        key = [version, str(table), "obs", column]
        values = cache.array(tuple(key), lambda: src.obs(column), lambda v: int(v.values.nbytes))
        return values, key

    values, key = _with_source(request, id, table, load)
    return _binary(request, values.values, key, x_dbx_kind=values.kind)


@router.get("/{id}/obsm")
def obsm(
    request: Request,
    id: str,
    key: Annotated[str, Query(max_length=512)],
    dims: Annotated[str, Query(pattern=r"^\d{1,4}(,\d{1,4}){0,2}$")] = "0,1",
    table: TableQ = None,
) -> Response:
    _, cache = _state(request)
    d = [int(x) for x in dims.split(",")]

    def load(src: Source, version: str) -> tuple[np.ndarray, list[str]]:
        parts = [version, str(table), "obsm", key, dims]
        arr = cache.array(tuple(parts), lambda: src.obsm(key, d), lambda a: int(a.nbytes))
        return arr, parts

    try:
        arr, parts = _with_source(request, id, table, load)
    except ValueError as e:
        raise _fail(400, "invalid_dims", "dims must be distinct columns of the embedding.") from e
    return _binary(request, arr, parts)
