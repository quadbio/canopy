"""Gene expression for the preview path: which matrices can be read per gene, the gene list, and
one gene's values encoded for the browser.

How a gene is read is decided from the fingerprint (no file access), by the bytes one gene
touches on disk:
- CSC (gene-major): one column is one contiguous slice ("direct");
- dense: one column touches every chunk that spans it; "direct" only when that is at most
  `DIRECT_BYTES` (narrow chunks), else the matrix is loaded whole ("memory") if it fits;
- CSR (cell-major): one gene needs the whole matrix, so it is built once into a float32 CSC
  copy in memory, block by block, if that fits ("memory");
- otherwise "needs_conversion".
"Fits" means the float32 copy is at most `memory.small_file_gb` and half the cache budget.

Values are sent sparse (uint32 cell indices, then values) when that is smaller than one value
per cell, else dense; optionally as uint16 codes, value = offset + code * scale, exact for
integer values within 65,534 of each other, with 0 always exact.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from dbx.inspect.models import AnnDataInfo, MatrixInfo

Access = Literal["direct", "memory", "needs_conversion"]

# A dense column read touching more than this is not "direct" (on network storage it would
# take seconds to minutes per gene).
DIRECT_BYTES = 64 * 2**20
_PAGE = 4096  # an unchunked (contiguous) dense matrix: one page per cell for one column
MISSING_CODE = 65535

# var columns that usually hold readable gene symbols when the index holds ids.
_SYMBOL_COLUMNS = (
    "gene_symbols",
    "gene_symbol",
    "symbol",
    "symbols",
    "feature_name",
    "gene_name",
    "gene_names",
    "gene",
    "genes",
    "name",
)
# Gene ids of common databases: Ensembl (all species), FlyBase, WormBase, TAIR, Entrez numbers.
_GENE_ID = re.compile(r"^(ENS[A-Z]*G\d{6,}|FBgn\d+|WBGene\d+|AT[1-5CM]G\d+|\d+)(\.\d+)?$")


def matrices(info: AnnDataInfo) -> dict[str, MatrixInfo]:
    """The matrices with one column per gene: X, then the layers."""
    out: dict[str, MatrixInfo] = {}
    if info.X is not None:
        out["X"] = info.X
    out.update(info.layers)
    return {k: m for k, m in out.items() if len(m.shape) == 2 and m.shape[1] == info.n_vars}


def loaded_bytes(m: MatrixInfo) -> int:
    """Size of the in-memory copy used for gene reads: float32 values, int32/int64 row indices."""
    n_obs, n_vars = m.shape
    if m.encoding == "dense":
        return n_obs * n_vars * 4
    idx = 4 if n_obs < 2**31 else 8
    return (m.nnz or 0) * (4 + idx) + (n_vars + 1) * 8


def column_read_bytes(m: MatrixInfo) -> int:
    """Estimated bytes read (and decompressed) from storage for one gene."""
    n_obs, n_vars = m.shape
    item = np.dtype(m.dtype).itemsize
    if m.encoding == "csc":
        return int((m.nnz or 0) / max(n_vars, 1) * (item + 4)) + 16
    if m.encoding == "csr":
        return loaded_bytes(m)  # the whole matrix
    chunks = m.storage.chunks
    if chunks and len(chunks) == 2:
        rows, cols = chunks
        return math.ceil(n_obs / rows) * rows * min(cols, n_vars) * item
    return min(n_obs * n_vars * item, n_obs * max(item, min(n_vars * item, _PAGE)))


def access(m: MatrixInfo, memory_limit: int) -> Access:
    if m.encoding == "csc":
        return "direct"
    if m.encoding == "dense" and column_read_bytes(m) <= DIRECT_BYTES:
        return "direct"
    return "memory" if loaded_bytes(m) <= memory_limit else "needs_conversion"


def load_peak_bytes(m: MatrixInfo, block_bytes: int) -> int:
    """Memory needed while building the in-memory copy: the copy plus one block of input."""
    return loaded_bytes(m) + block_bytes


@dataclass
class GeneList:
    names: list[str]  # the var index made unique (what requests and saved settings use)
    symbols: list[str] | None  # readable symbols, when the index holds ids
    symbol_column: str | None
    duplicates: int  # names that had to be made unique


def unique_names(names: list[str]) -> tuple[list[str], int]:
    """anndata's `var_names_make_unique`: the 2nd `A` becomes `A-1`, the 3rd `A-2`, …"""
    seen: dict[str, int] = {}
    taken = set(names)
    out: list[str] = []
    dup = 0
    for n in names:
        if n not in seen:
            seen[n] = 0
            out.append(n)
            continue
        dup += 1
        k = seen[n]
        while True:
            k += 1
            cand = f"{n}-{k}"
            if cand not in taken:
                break
        seen[n] = k
        taken.add(cand)
        out.append(cand)
    return out, dup


def gene_list(var: pd.DataFrame) -> GeneList:
    names, dup = unique_names([str(x) for x in var.index])
    lower = {str(c).lower(): c for c in var.columns}
    sample = names[:200]
    ids_like = sum(bool(_GENE_ID.match(n)) for n in sample) > len(sample) // 2
    if ids_like:
        for key in _SYMBOL_COLUMNS:
            col = lower.get(key)
            if col is None:
                continue
            s = var[col]
            if pd.api.types.is_numeric_dtype(s.dtype) or pd.api.types.is_bool_dtype(s.dtype):
                continue
            symbols = ["" if pd.isna(x) else str(x) for x in s]
            if symbols != names and sum(bool(x) for x in symbols) > len(symbols) // 2:
                return GeneList(names, symbols, str(col), dup)
    return GeneList(names, None, None, dup)


@dataclass
class GeneValues:
    """One gene across all cells: the cells with a stored value (usually the non-zero ones)."""

    n: int
    indices: np.ndarray  # int64, sorted, unique
    values: np.ndarray  # float32


def from_dense(col: np.ndarray) -> GeneValues:
    col = np.asarray(col, dtype=np.float32).ravel()
    nz = np.flatnonzero(col != 0)  # NaN != 0: kept
    return GeneValues(len(col), nz.astype(np.int64), col[nz])


def from_column(indices: np.ndarray, data: np.ndarray, n: int) -> GeneValues:
    """One column of a CSC matrix: its row indices and values. Rows listed twice (allowed in
    non-canonical files) are summed, as scipy and anndata do."""
    idx = np.asarray(indices, dtype=np.int64)
    vals = np.asarray(data, dtype=np.float32)
    order = np.argsort(idx, kind="stable")  # usually sorted already
    idx, vals = idx[order], vals[order]
    if len(idx) > 1 and np.any(idx[1:] == idx[:-1]):
        uniq, start = np.unique(idx, return_index=True)
        vals = np.add.reduceat(vals, start).astype(np.float32)
        idx = uniq
    return GeneValues(n, idx, vals)


def from_loaded(m: Any, i: int) -> GeneValues:
    """Gene `i` of an in-memory copy: scipy CSC, or a dense (column-major) ndarray."""
    if isinstance(m, np.ndarray):
        return from_dense(m[:, i])
    a, b = int(m.indptr[i]), int(m.indptr[i + 1])
    return from_column(m.indices[a:b], m.data[a:b], m.shape[0])


@dataclass
class Stats:
    min: float  # over all cells (unstored cells count as 0), finite values only
    max: float
    p99: float  # 99th percentile of the non-zero finite values (colour scale top)
    n_expressing: int  # cells with a non-zero finite value


def stats(g: GeneValues) -> Stats:
    v = g.values[np.isfinite(g.values)]
    nonzero = v[v != 0]
    implicit_zeros = g.n > len(g.values)
    lo = float(v.min()) if len(v) else 0.0
    hi = float(v.max()) if len(v) else 0.0
    if implicit_zeros or not len(v):
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    p99 = float(np.percentile(nonzero, 99)) if len(nonzero) else hi
    return Stats(min=lo, max=hi, p99=p99, n_expressing=int(len(nonzero)))


@dataclass
class Encoded:
    body: bytes
    headers: dict[str, str]


def quantise(vals: np.ndarray) -> tuple[np.ndarray, float, float]:
    """uint16 codes, offset and scale. Integers within 65,534 of each other are exact (scale 1);
    otherwise 0 falls exactly on a code. NaN is MISSING_CODE; ±inf the end codes."""
    finite = vals[np.isfinite(vals)]
    lo = float(finite.min()) if len(finite) else 0.0
    hi = float(finite.max()) if len(finite) else 0.0
    lo, hi = min(lo, 0.0), max(hi, 0.0)  # 0 inside the range, so it can be exact
    top = MISSING_CODE - 1
    if len(finite) and np.all(finite == np.round(finite)) and hi - lo <= top:
        offset, scale = lo, 1.0
    else:
        scale = (hi - lo) / (top - 1) if hi > lo else 1.0  # one spare step for the anchoring
        offset = -round(-lo / scale) * scale  # a whole number of steps below 0
    codes = np.empty(len(vals), dtype="<u2")
    with np.errstate(invalid="ignore"):
        q = np.rint((np.nan_to_num(vals, nan=0.0, posinf=hi, neginf=lo) - offset) / scale)
    codes[:] = np.clip(q, 0, top)
    codes[np.isnan(vals)] = MISSING_CODE
    return codes, offset, scale


def encode(g: GeneValues, quantise_values: bool) -> Encoded:
    """Sparse or dense, float32 or uint16; headers describe it (see module docstring)."""
    if g.n >= 2**32:
        raise ValueError("more than 2^32 cells: uint32 cell indices would wrap")
    st = stats(g)
    value_bytes = 2 if quantise_values else 4
    sparse = len(g.indices) * (4 + value_bytes) < g.n * value_bytes
    if sparse:
        vals = g.values
    else:
        vals = np.zeros(g.n, dtype=np.float32)
        vals[g.indices] = g.values
    headers = {
        "x-dbx-encoding": "sparse" if sparse else "dense",
        "x-dbx-n": str(g.n),
        "x-dbx-nnz": str(len(g.indices)),
        "x-dbx-n-expressing": str(st.n_expressing),
        "x-dbx-min": repr(st.min),
        "x-dbx-max": repr(st.max),
        "x-dbx-p99": repr(st.p99),
    }
    payload: np.ndarray
    if quantise_values:
        payload, offset, scale = quantise(vals)
        headers |= {"x-dbx-offset": repr(offset), "x-dbx-scale": repr(scale)}
    else:
        payload = np.ascontiguousarray(vals, dtype="<f4")
    headers["x-dbx-dtype"] = payload.dtype.name
    body = payload.tobytes()
    if sparse:
        body = np.ascontiguousarray(g.indices, dtype="<u4").tobytes() + body
    return Encoded(body, headers)


def decode(body: bytes, headers: dict[str, str]) -> np.ndarray:
    """Inverse of `encode`, dense float32 (used by tests and by `dbx` tools)."""
    n, nnz = int(headers["x-dbx-n"]), int(headers["x-dbx-nnz"])
    sparse = headers["x-dbx-encoding"] == "sparse"
    dtype = np.dtype("<u2") if headers["x-dbx-dtype"] == "uint16" else np.dtype("<f4")
    off = 4 * nnz if sparse else 0
    raw = np.frombuffer(body, dtype=dtype, offset=off, count=nnz if sparse else n)
    if dtype.kind == "u":
        vals = float(headers["x-dbx-offset"]) + raw.astype(np.float64) * float(
            headers["x-dbx-scale"]
        )
        vals = np.where(raw == MISSING_CODE, np.nan, vals).astype(np.float32)
    else:
        vals = raw.astype(np.float32)
    if not sparse:
        return vals
    out = np.zeros(n, dtype=np.float32)
    out[np.frombuffer(body, dtype="<u4", count=nnz)] = vals
    return out
