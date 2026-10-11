"""Dataset API: metadata as JSON, arrays as binary.

Binary replies are little-endian typed arrays (`application/octet-stream`) described by headers:
`x-dbx-dtype` (numpy name: float32, int8, int16, int32), `x-dbx-shape` (comma-separated) and,
for obs columns, `x-dbx-kind`. Category codes (also booleans: 0 false, 1 true) use -1 for
missing. A weak ETag (dataset version + request + API format) lets browsers revalidate with a
304, decided before anything is read. Float arrays are sent uncompressed (gzip saves ~7 % on
floats and costs ~0.2 s per million values); JSON and integer codes are gzipped.

Errors use the app-wide envelope `{"detail": {"code", "message"}, "request_id"}`:
404 unknown dataset/element, 400 invalid dims, 409 not previewable (e.g.
`needs_conversion:legacy_layout`), 410 vanished, 403 path left its data root. Anything else is
a logged 500.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import numpy as np
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from dbx.catalog import Catalog, PathEscapeError
from dbx.catalog.db import Row
from dbx.data import genes
from dbx.data.cache import DatasetCache, MemoryBusyError, table_info
from dbx.data.reader import (
    ColumnMeta,
    InvalidDimsError,
    NeedsConversionError,
    ObsValues,
    Source,
    SourceClosedError,
    UnknownElementError,
    block_bytes,
    embedding_dims,
    valid_colors,
)
from dbx.errors import ApiError
from dbx.inspect.core import version_stamp
from dbx.inspect.models import AnnDataInfo, Fingerprint
from dbx.logs import get_logger

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

# Bump when the encoding of any binary reply changes, so cached browser copies are not reused.
API_FORMAT = "2"


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
    matrices: list[Matrix]
    embeddings: list[Embedding]
    warnings: list[str]


class Matrix(BaseModel):
    name: str  # "X" or a layer name
    encoding: str  # csr | csc | dense
    dtype: str
    access: genes.Access  # how genes are read (see dbx.data.genes)


class GeneListReply(BaseModel):
    names: list[str]
    symbols: list[str] | None
    symbol_column: str | None
    duplicates: int  # var names that were made unique (`A`, `A-1`)
    matrices: list[Matrix]


class Labels(BaseModel):
    kind: str
    categories: list[str] | None
    # The dataset's own colours for these labels (`uns['<col>_colors']`), e.g. for a text column
    # that scanpy turned into categories (alphabetical, as here) when plotting.
    colors: list[str] | None = None


def _state(request: Request) -> tuple[Catalog, DatasetCache]:
    return request.app.state.catalog, request.app.state.datasets


@lru_cache(maxsize=256)
def _fingerprint(version: str, text: str) -> Fingerprint:
    """Parsed once per dataset version (the JSON can be large for SpatialData)."""
    return Fingerprint.model_validate_json(text)


def _dataset(request: Request, id: str) -> tuple[Row, Fingerprint, Path]:
    catalog, _ = _state(request)
    row = catalog.get(id)
    if row is None:
        raise ApiError(404, "unknown_dataset", "No dataset with this id in the catalog.")
    if row.state != "inspected" or not row.fingerprint or not row.version:
        raise ApiError(409, f"state:{row.state}", f"The dataset is {row.state} ({row.reason}).")
    try:
        path = catalog.resolve(row)
    except PathEscapeError as e:
        raise ApiError(403, "path_escape", "The dataset path leaves its data root.") from e
    except FileNotFoundError as e:
        catalog.refresh()
        raise ApiError(410, "vanished", "The dataset no longer exists; rescanning.") from e
    return row, _fingerprint(row.version, row.fingerprint), path


def _check_current(catalog: Catalog, row: Row, path: Path) -> None:
    """Rows describe the file as it was at the last scan; ask for a rescan if it changed (the
    request is still served from the file as it is now)."""
    try:
        if version_stamp(path) != row.stamp:
            catalog.refresh()
    except OSError:
        pass


def _with_source[T](
    request: Request,
    row: Row,
    fp: Fingerprint,
    path: Path,
    table: str | None,
    fn: Callable[[Source], T],
) -> T:
    """Run `fn(source)` on the dataset's open source (opened on first use)."""
    catalog, cache = _state(request)
    version = str(row.version)
    if not cache.has_source(version, table):
        _check_current(catalog, row, path)
    for attempt in (0, 1):
        try:
            return fn(cache.source(version, fp, path, table, dataset=row.id))
        except SourceClosedError:
            if attempt:
                raise
            cache.drop_closed_source(version, table)
        except NeedsConversionError as e:
            raise ApiError(
                409, f"needs_conversion:{e.code}", "Convert the dataset to view this."
            ) from e
        except UnknownElementError as e:
            what = e.args[0]
            code = f"unknown_{what}" if what in ("matrix", "gene") else "unknown_element"
            raise ApiError(404, code, f"No such {what} in this dataset.") from e
        except MemoryBusyError as e:
            raise ApiError(
                503, "memory_busy", "The server is short of memory right now; try again soon."
            ) from e
        except InvalidDimsError as e:
            raise ApiError(
                400, "invalid_dims", "dims must be distinct columns of the embedding."
            ) from e
        except FileNotFoundError as e:
            catalog.refresh()
            raise ApiError(410, "vanished", "The dataset no longer exists.") from e
    raise AssertionError("unreachable")


def _etag(parts: list[str]) -> str:
    return 'W/"' + hashlib.sha256("|".join([API_FORMAT, *parts]).encode()).hexdigest()[:20] + '"'


def _not_modified(request: Request, etag: str) -> Response | None:
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304, headers={"etag": etag, "cache-control": "private, no-cache"}
        )
    return None


def _binary(arr: np.ndarray, etag: str, **headers: str) -> Response:
    arr = np.ascontiguousarray(arr)
    if arr.dtype.byteorder == ">":
        arr = arr.astype(arr.dtype.newbyteorder("<"))
    extra = {k.replace("_", "-"): v for k, v in headers.items()}
    if arr.dtype.kind == "f":
        # Already-encoded replies are left alone by the gzip middleware.
        extra["content-encoding"] = "identity"
    return Response(
        content=arr.tobytes(),
        media_type="application/octet-stream",
        headers={
            "etag": etag,
            "cache-control": "private, no-cache",
            "x-dbx-dtype": arr.dtype.name,
            "x-dbx-shape": ",".join(str(n) for n in arr.shape),
            **extra,
        },
    )


TableQ = Annotated[str | None, Query(max_length=512)]
NameQ = Annotated[str, Query(min_length=1, max_length=512)]


@router.get("/{id}/meta")
def meta(request: Request, id: str, table: TableQ = None) -> DatasetMeta:
    row, fp, path = _dataset(request, id)
    try:
        name, info = table_info(fp, table)
    except NeedsConversionError as e:
        raise ApiError(409, f"needs_conversion:{e.code}", "This dataset has no table.") from e
    except UnknownElementError as e:
        raise ApiError(404, "unknown_element", "No such table in this dataset.") from e

    def describe(src: Source) -> tuple[str, list[ColumnMeta]]:
        return src.mode, [src.column_meta(c.name) for c in info.obs.columns]

    mode, columns = _with_source(request, row, fp, path, name, describe)
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
        matrices=_matrices(request, info),
        warnings=sorted({w.code for w in fp.warnings}),
    )


def _obs_values(
    request: Request, row: Row, fp: Fingerprint, path: Path, table: str | None, column: str
) -> ObsValues:
    _, cache = _state(request)
    return _with_source(
        request,
        row,
        fp,
        path,
        table,
        lambda src: cache.array(
            (str(row.version), str(table), "obs", column),
            lambda: src.obs(column),
            lambda v: int(v.values.nbytes),
            what="obs",
            dataset_id=row.id,
        ),
    )


@router.get("/{id}/obs")
def obs(request: Request, id: str, column: NameQ, table: TableQ = None) -> Response:
    row, fp, path = _dataset(request, id)
    table, _ = _table(fp, table)  # the resolved name: one cache entry per table
    etag = _etag([str(row.version), str(table), "obs", column])
    if (r := _not_modified(request, etag)) is not None:
        return r
    values = _obs_values(request, row, fp, path, table, column)
    return _binary(values.values, etag, x_dbx_kind=values.kind)


@router.get("/{id}/obs/labels")
def obs_labels(request: Request, id: str, column: NameQ, table: TableQ = None) -> Labels:
    """The category labels behind a column's codes, also for plain string columns (whose labels
    are only known after reading the column; categorical columns have them in /meta too)."""
    row, fp, path = _dataset(request, id)
    table, _ = _table(fp, table)
    values = _obs_values(request, row, fp, path, table, column)
    colors = None
    if values.categories and values.kind in ("categorical", "string"):
        raw = _with_source(request, row, fp, path, table, lambda src: src.uns_colors(column))
        if raw is not None:
            colors = valid_colors(raw, len(values.categories), column)
    return Labels(kind=values.kind, categories=values.categories, colors=colors)


@router.get("/{id}/obsm")
def obsm(
    request: Request,
    id: str,
    key: NameQ,
    dims: Annotated[str, Query(pattern=r"^\d{1,4}(,\d{1,4}){0,2}$")] = "0,1",
    table: TableQ = None,
) -> Response:
    row, fp, path = _dataset(request, id)
    table, _ = _table(fp, table)
    etag = _etag([str(row.version), str(table), "obsm", key, dims])
    if (r := _not_modified(request, etag)) is not None:
        return r
    _, cache = _state(request)
    d = [int(x) for x in dims.split(",")]
    arr = _with_source(
        request,
        row,
        fp,
        path,
        table,
        lambda src: cache.array(
            (str(row.version), str(table), "obsm", key, dims),
            lambda: src.obsm(key, d),
            lambda a: int(a.nbytes),
            what="obsm",
            dataset_id=row.id,
        ),
    )
    return _binary(arr, etag)


# ---------------------------------------------------------------- genes


def _memory_limit(request: Request) -> int:
    """Largest in-memory matrix copy built for gene queries: the small-file limit, and at most
    half the cache budget (the copy is cached like any array)."""
    _, cache = _state(request)
    return min(cache.small_file, cache.budget // 2)


def _matrices(request: Request, info: AnnDataInfo) -> list[Matrix]:
    limit = _memory_limit(request)
    return [
        Matrix(name=k, encoding=m.encoding, dtype=m.dtype, access=genes.access(m, limit))
        for k, m in genes.matrices(info).items()
    ]


@router.get("/{id}/genes")
def gene_list(request: Request, id: str, table: TableQ = None) -> Response:
    """Gene names (the var index, made unique), readable symbols when the index holds ids, and
    the matrices genes can be read from."""
    row, fp, path = _dataset(request, id)
    tname, info = _table(fp, table)
    etag = _etag([str(row.version), str(tname), "genes"])
    if (r := _not_modified(request, etag)) is not None:
        return r
    _, cache = _state(request)
    found = _with_source(
        request,
        row,
        fp,
        path,
        tname,
        lambda src: cache.array(
            (str(row.version), str(tname), "genes"),
            lambda: genes.gene_list(src.var()),
            lambda g: 64 * len(g.names),
            what="genes",
            dataset_id=row.id,
        ),
    )
    reply = GeneListReply(
        names=found.names,
        symbols=found.symbols,
        symbol_column=found.symbol_column,
        duplicates=found.duplicates,
        matrices=_matrices(request, info),
    )
    return Response(
        content=reply.model_dump_json(),
        media_type="application/json",
        headers={"etag": etag, "cache-control": "private, no-cache"},
    )


@router.get("/{id}/gene")
def gene(
    request: Request,
    id: str,
    i: Annotated[int, Query(ge=0, le=10_000_000)],
    matrix: Annotated[str, Query(min_length=1, max_length=512)] = "X",
    q: Annotated[int, Query(description="16: values as uint16 (see x-dbx-scale)")] = 0,
    table: TableQ = None,
) -> Response:
    """One gene's values across all cells (binary, see dbx.data.genes for the encoding)."""
    if q not in (0, 16):
        raise ApiError(400, "invalid_q", "q must be 0 or 16.")
    row, fp, path = _dataset(request, id)
    tname, info = _table(fp, table)
    etag = _etag([str(row.version), str(tname), "gene", matrix, str(i), str(q)])
    if (r := _not_modified(request, etag)) is not None:
        return r
    m = genes.matrices(info).get(matrix)
    if m is None:
        raise ApiError(404, "unknown_matrix", "No such matrix in this dataset.")
    if i >= info.n_vars:
        raise ApiError(404, "unknown_gene", "No such gene in this dataset.")
    acc = genes.access(m, _memory_limit(request))
    # Facts for the log line of this request (names only as name fields: tokenised at strict).
    facts: dict[str, Any] = {
        "element": matrix,
        "gene_index": i,
        "layout": m.encoding,
        "access": acc,
        "est_read_bytes": genes.column_read_bytes(m) if acc == "direct" else 0,
    }
    log = get_logger("dbx.genes")
    if acc == "needs_conversion":
        log.info("gene_refused", dataset_id=row.id, loaded_bytes=genes.loaded_bytes(m), **facts)
        raise ApiError(
            409,
            f"needs_conversion:matrix_{m.encoding}",
            "Gene expression from this matrix needs a one-time conversion.",
        )
    _, cache = _state(request)
    version = str(row.version)
    loaded: dict[str, object] = {}

    def read(src: Source) -> genes.GeneValues:
        if acc == "direct":
            return src.gene(matrix, i)
        copy = cache.array(
            (version, str(tname), "columns", matrix),
            lambda: src.matrix_columns(matrix),
            _columns_bytes,
            reserve=genes.load_peak_bytes(m, block_bytes(m)),
            outcome=loaded,
            what="matrix_columns",
            dataset_id=row.id,
            layout=m.encoding,
            element=matrix,
        )
        return genes.from_loaded(copy, i)

    outcome: dict[str, object] = {}
    values = _with_source(
        request,
        row,
        fp,
        path,
        tname,
        lambda src: cache.array(
            (version, str(tname), "gene", matrix, i),
            lambda: read(src),
            lambda g: int(g.indices.nbytes + g.values.nbytes),
            outcome=outcome,
            what="gene",
            dataset_id=row.id,
            **facts,
        ),
    )
    enc = genes.encode(values, quantise_values=q == 16)
    log.info(
        "gene_served",
        dataset_id=row.id,
        cache=outcome.get("cache"),
        matrix_cache=loaded.get("cache"),
        q=q,
        encoding=enc.headers["x-dbx-encoding"],
        n_cells=values.n,
        nnz=len(values.indices),
        n_expressing=int(enc.headers["x-dbx-n-expressing"]),
        reply_bytes=len(enc.body),
        **facts,
    )
    return Response(
        content=enc.body,
        media_type="application/octet-stream",
        headers={
            "etag": etag,
            "cache-control": "private, no-cache",
            "content-encoding": "identity",
            **enc.headers,
        },
    )


def _columns_bytes(copy: object) -> int:
    if isinstance(copy, np.ndarray):
        return int(copy.nbytes)
    c: Any = copy
    return int(c.data.nbytes + c.indices.nbytes + c.indptr.nbytes)


def _table(fp: Fingerprint, table: str | None) -> tuple[str | None, AnnDataInfo]:
    try:
        name, info = table_info(fp, table)
    except NeedsConversionError as e:
        raise ApiError(409, f"needs_conversion:{e.code}", "This dataset has no table.") from e
    except UnknownElementError as e:
        raise ApiError(404, "unknown_element", "No such table in this dataset.") from e
    return name, info
