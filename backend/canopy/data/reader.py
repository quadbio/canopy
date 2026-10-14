"""Read one AnnData table (h5ad, AnnData-zarr or a SpatialData table) without conversion.

Two sources behind one interface:
- `LazySource` reads single elements (one obs column, one embedding, one colour list) straight
  from the h5py/zarr group with anndata's element reader; nothing else is loaded. Used for the
  current on-disk layout, whatever the file size.
- `MemorySource` holds a fully loaded AnnData. Used for legacy layouts (anndata < 0.8), which
  only anndata's whole-file reader understands, and only when the estimated in-memory size is
  below `memory.small_file_gb`; larger legacy files need conversion.

Values returned are what the API serves: little-endian float32 embeddings, category codes, and
float32/uint8 numeric/boolean columns.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
from anndata.io import read_elem
from scipy import sparse

from canopy.data.genes import GeneValues, from_column, from_dense, matrices
from canopy.inspect.models import AnnDataInfo, MatrixInfo
from canopy.logs import get_logger

# Categories beyond this are not sent (and the column cannot be coloured by category).
MAX_CATEGORIES = 10_000
TEXT_SAMPLE = 100_000  # values sampled before a text column is read whole
_HEX = re.compile(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?")

ObsKind = Literal["categorical", "numeric", "boolean", "string", "other"]
_log = get_logger("canopy.data")


class UnknownElementError(LookupError):
    """The request names a table, column or embedding the dataset does not have. Raised only by
    canopy's own membership checks, so a KeyError from inside a library is never mistaken for it."""

    expected = True  # a refusal answered with a code, not a fault (logs.timed)
    code = "unknown_element"


class InvalidDimsError(ValueError):
    """The requested embedding columns are not distinct columns of that embedding."""

    expected = True
    code = "invalid_dims"


class SourceClosedError(Exception):
    """The source was evicted from the cache while a request was using it; open it again."""


class NeedsConversionError(Exception):
    """The dataset cannot be previewed directly; the reason is a short code."""

    expected = True

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def log_code(self) -> str:
        """As the server answers it (`needs_conversion:<reason>`): a code `known_code` knows."""
        return f"needs_conversion:{self.code}"


@dataclass
class ObsValues:
    kind: ObsKind
    values: np.ndarray  # codes (int8/16/32, -1 = missing), float32 or uint8
    categories: list[str] | None = None


@dataclass
class ColumnMeta:
    name: str
    kind: ObsKind
    dtype: str
    n_categories: int | None = None
    ordered: bool | None = None
    categories: list[str] | None = None
    colors: list[str] | None = None


def _codes_dtype(n: int) -> type[np.signedinteger[Any]]:
    if n < 2**7:
        return np.int8
    if n < 2**15:
        return np.int16
    return np.int32


def _categorical(cat: pd.Categorical) -> ObsValues:
    cats = [str(c) for c in cat.categories]
    codes = np.asarray(cat.codes).astype(_codes_dtype(len(cats)), copy=False)
    return ObsValues("categorical", codes, cats)


def to_obs_values(raw: Any) -> ObsValues:
    """An obs column as read by anndata (Categorical, ndarray, masked array) to served values."""
    if isinstance(raw, pd.Series):
        raw = raw.array
    if isinstance(raw, pd.Categorical):
        return _categorical(raw)
    if isinstance(raw, pd.api.extensions.ExtensionArray):
        if pd.api.types.is_bool_dtype(raw.dtype):
            # int8 codes like categories: 0 false, 1 true, -1 missing
            mask = np.asarray(raw.isna())
            codes = np.asarray(raw.fillna(False), dtype=np.int8)
            codes[mask] = -1
            return ObsValues("boolean", codes, ["false", "true"])
        if pd.api.types.is_numeric_dtype(raw.dtype):
            wide = raw.to_numpy(dtype=np.float64, na_value=np.nan)
            # nullable whole numbers beyond float32's exact range: float64, as for plain ints
            # (review full-2 B-L1)
            big = pd.api.types.is_integer_dtype(raw.dtype) and bool(
                np.nanmax(np.abs(wide), initial=0) >= 2**24
            )
            return ObsValues("numeric", wide.astype("<f8" if big else "<f4"))
        raw = np.asarray(raw, dtype=object)
    arr = np.asarray(raw)
    if arr.dtype.kind == "b":
        return ObsValues("boolean", arr.astype(np.int8), ["false", "true"])
    if arr.dtype.kind in "iu" and arr.size and int(np.abs(arr).max()) >= 2**24:
        # whole numbers beyond float32's exact range (ids, counts): sent as float64, so a hover
        # shows the right number
        return ObsValues("numeric", arr.astype("<f8"))
    if arr.dtype.kind in "iuf":
        return ObsValues("numeric", arr.astype("<f4"))
    if arr.dtype.kind in "OSUT":
        codes, uniques = pd.factorize(pd.Series(arr, dtype=object), use_na_sentinel=True)
        if len(uniques) > MAX_CATEGORIES:
            raise NeedsConversionError("too_many_values")
        cats = [str(u) for u in uniques]
        order = np.argsort(cats, kind="stable")
        remap = np.empty(len(cats) + 1, dtype=np.int64)
        remap[order] = np.arange(len(cats))
        remap[-1] = -1
        codes = remap[codes]
        return ObsValues("string", codes.astype(_codes_dtype(len(cats))), [cats[i] for i in order])
    raise NeedsConversionError("unsupported_column")


def to_hex(colour: str) -> str | None:
    """A colour as `#rrggbb` (alpha dropped): hex (`#rgb`, `#rrggbb`, `#rrggbbaa`) or any name
    matplotlib understands (`red`, `lightgray`, `tab:blue`, `C0`, …); None if not a colour."""
    c = colour.strip()
    if _HEX.fullmatch(c):
        return c[:7].lower()  # SPEC: "#rrggbb"
    if re.fullmatch(r"#[0-9a-fA-F]{3}", c):
        return "#" + "".join(ch * 2 for ch in c[1:]).lower()
    try:
        from matplotlib.colors import to_hex as mpl_to_hex
    except ImportError:  # matplotlib comes with the data stack; without it, hex only
        return None
    try:
        return str(mpl_to_hex(c, keep_alpha=False))
    except ValueError:
        return None


def parse_colors(raw: Any, n: int) -> tuple[list[str] | None, str | None]:
    """`uns['<col>_colors']` as one `#rrggbb` per category, or (None, why not). Like scanpy, a
    longer list is used for its first `n` colours; a shorter one cannot be used."""
    try:
        cols = [c.decode() if isinstance(c, bytes) else str(c) for c in np.asarray(raw).ravel()]
    except (TypeError, ValueError):
        return None, "unreadable"
    if len(cols) < n:
        return None, "too_few"
    out = [to_hex(c) for c in cols[:n]]
    if any(h is None for h in out):
        return None, "not_a_colour"
    return [h for h in out if h is not None], None


def valid_colors(raw: Any, n: int, column: str = "") -> list[str] | None:
    """The dataset's own colours for a column (see `parse_colors`); a list that cannot be used
    is logged with the reason, since the browser then makes up a palette instead."""
    cols, why = parse_colors(raw, n)
    if why is not None:
        _log.info(
            "uns_colors_ignored",
            column=column,
            reason=why,
            n_categories=n,
            n_colors=int(np.asarray(raw).size),
        )
    return cols


def embedding_dims(info: AnnDataInfo) -> dict[str, int]:
    """obsm keys usable as embeddings (one row per cell, at least 2 columns), X_umap first."""
    out = {
        k: a.shape[1]
        for k, a in info.obsm.items()
        if len(a.shape) == 2 and a.shape[0] == info.n_obs and a.shape[1] >= 2
    }
    order = sorted(out, key=lambda k: (k != "X_umap", k.lower()))
    return {k: out[k] for k in order}


def _matrix_bytes(m: MatrixInfo | None) -> int:
    if m is None:
        return 0
    item = np.dtype(m.dtype).itemsize
    if m.encoding == "dense":
        return int(np.prod(m.shape)) * item
    idx = np.dtype(m.indices_dtype or "int32").itemsize
    return (m.nnz or 0) * (item + idx) + (m.shape[0] + 1) * 8


def estimate_memory_bytes(info: AnnDataInfo) -> int:
    """Rough in-memory size of a fully loaded AnnData (X, layers, raw, obsm, obs/var)."""
    total = _matrix_bytes(info.X) + _matrix_bytes(info.raw)
    total += sum(_matrix_bytes(m) for m in info.layers.values())
    total += sum(_matrix_bytes(m) for m in info.obsp.values())
    for a in (*info.obsm.values(), *info.varm.values()):
        total += int(np.prod(a.shape)) * np.dtype(a.dtype).itemsize
    total += 16 * (
        info.n_obs * (len(info.obs.columns) + 1) + info.n_vars * (len(info.var.columns) + 1)
    )
    return total


class Source:
    """One AnnData table. Methods are thread-safe (HDF5 access is serialised anyway)."""

    def __init__(self, info: AnnDataInfo) -> None:
        self.info = info
        self.store_build: str | None = None  # a converted store's build key
        self.lock = threading.Lock()
        self.closed = False

    mode: str = ""

    def obs(self, name: str) -> ObsValues:
        raise NotImplementedError

    def obsm(self, key: str, dims: list[int]) -> np.ndarray:
        raise NotImplementedError

    def column_meta(self, name: str) -> ColumnMeta:
        raise NotImplementedError

    def var(self) -> pd.DataFrame:
        """The var table (gene names and annotations)."""
        raise NotImplementedError

    def uns_colors(self, name: str) -> Any:
        """`uns['<name>_colors']` as stored, or None."""
        raise NotImplementedError

    def stored_subset(self, n: int) -> np.ndarray | None:
        """A converted store's precomputed subset of `n` cells (SPEC §4.5), if there is one."""
        return None

    def gene(self, matrix: str, i: int) -> GeneValues:
        """Gene `i` of a CSC or dense matrix, read directly."""
        raise NotImplementedError

    def matrix_columns(self, matrix: str) -> Any:
        """The whole matrix in memory, in a form where a gene is one cheap slice: float32 scipy
        CSC (sparse input) or a column-major float32 ndarray (dense input)."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def _check_gene(info: AnnDataInfo, matrix: str, i: int) -> MatrixInfo:
    m = matrices(info).get(matrix)
    if m is None:
        raise UnknownElementError("matrix")
    if not 0 <= i < info.n_vars:
        raise UnknownElementError("gene")
    return m


# Input read per block while building an in-memory copy (bounds the extra memory of a load).
BLOCK_VALUES = 4_000_000


def block_bytes(m: MatrixInfo) -> int:
    """Memory used besides the result while building it: one input block, its sorted and
    position arrays (about 8 int64 arrays of the block's length) and the read buffers, twice
    (measured: 300 k × 20 k, 0.96 GB copy, peak +1.42 GB)."""
    return 2 * BLOCK_VALUES * (np.dtype(m.dtype).itemsize + 8 * 8)


def csr_to_csc_blocks(
    indptr: np.ndarray,
    n_vars: int,
    read: Any,
) -> sparse.csc_matrix:
    """A float32 CSC copy of a CSR matrix, built block by block: `read(lo, hi)` returns the
    (indices, data) slices of the stored entries lo..hi. Two passes over the indices (count per
    column, then fill), so the peak is the result plus one block, never CSR + CSC together."""
    indptr = np.asarray(indptr, dtype=np.int64)
    n_obs = len(indptr) - 1
    nnz = int(indptr[-1])
    per_row = max(1.0, nnz / max(n_obs, 1))
    rows_per_block = max(1, int(BLOCK_VALUES / per_row))
    blocks = [(a, min(a + rows_per_block, n_obs)) for a in range(0, n_obs, rows_per_block)]
    counts = np.zeros(n_vars, dtype=np.int64)
    for a, b in blocks:
        idx, _ = read(int(indptr[a]), int(indptr[b]), values=False)
        counts += np.bincount(np.asarray(idx, dtype=np.int64), minlength=n_vars)
    col_ptr = np.zeros(n_vars + 1, dtype=np.int64)
    np.cumsum(counts, out=col_ptr[1:])
    out_idx = np.empty(nnz, dtype=np.int32 if n_obs < 2**31 else np.int64)
    out_data = np.empty(nnz, dtype=np.float32)
    fill = col_ptr[:-1].copy()
    for a, b in blocks:
        lo, hi = int(indptr[a]), int(indptr[b])
        idx, data = read(lo, hi, values=True)
        # Group the block's entries by column, rows staying ascending within a column (a stable
        # sort). With at most 65,536 genes the keys fit 16 bits, where numpy's stable sort is a
        # linear-time radix sort.
        keys = np.asarray(idx, dtype=np.uint16 if n_vars <= 2**16 else np.int64)
        order = np.argsort(keys, kind="stable")
        idx_s = keys[order].astype(np.int64)
        cnts = np.bincount(idx_s, minlength=n_vars)
        first = np.cumsum(cnts) - cnts  # where each column's entries start in this block
        pos = fill[idx_s] + (np.arange(len(idx_s)) - first[idx_s])
        rows = np.repeat(np.arange(a, b, dtype=np.int64), np.diff(indptr[a : b + 1]))
        out_idx[pos] = rows[order]
        out_data[pos] = np.asarray(data, dtype=np.float32)[order]
        fill += cnts
    return sparse.csc_matrix((out_data, out_idx, col_ptr), shape=(n_obs, n_vars))


def _check_dims(info: AnnDataInfo, key: str, dims: list[int]) -> None:
    n = embedding_dims(info).get(key)
    if n is None:
        raise UnknownElementError("embedding")
    if not dims or len(set(dims)) != len(dims) or any(d < 0 or d >= n for d in dims):
        raise InvalidDimsError("invalid dims")


class LazySource(Source):
    mode = "lazy"

    def __init__(self, info: AnnDataInfo, group: Any, handle: Any = None) -> None:
        """`group`: the h5py/zarr group of the AnnData table; `handle`: the file to close."""
        super().__init__(info)
        self.group = group
        self.handle = handle

    def _column_group(self, name: str) -> Any:
        if name not in {c.name for c in self.info.obs.columns}:
            raise UnknownElementError("column")
        return self.group["obs"][name]

    def obs(self, name: str) -> ObsValues:
        col = next((c for c in self.info.obs.columns if c.name == name), None)
        if col is not None and col.kind == "string" and self.info.n_obs > TEXT_SAMPLE:
            # a text column with many distinct values (barcodes, ids) is refused from a sample,
            # before the whole column is read
            def sample() -> int:
                node = self._column_group(name)
                if not hasattr(node, "shape") and "values" in node:
                    node = node["values"]  # nullable-string-array: values + mask
                if not hasattr(node, "shape"):
                    return 0
                return len(set(np.asarray(node[:TEXT_SAMPLE]).tolist()))

            if self._read(sample) > MAX_CATEGORIES:
                raise NeedsConversionError("too_many_values")
        return to_obs_values(self._read(lambda: read_elem(self._column_group(name))))

    def obsm(self, key: str, dims: list[int]) -> np.ndarray:
        _check_dims(self.info, key, dims)

        def read() -> np.ndarray:
            node = self.group["obsm"][key]
            if hasattr(node, "shape"):  # a plain array: read only the chosen columns
                cols = sorted(dims)
                block = np.asarray(node[:, cols])
                return np.asarray(block[:, [cols.index(d) for d in dims]])
            frame: Any = read_elem(node)  # a data frame
            cols = frame.iloc[:, dims]
            try:
                return np.asarray(cols.apply(pd.to_numeric, errors="raise").to_numpy())
            except (TypeError, ValueError) as e:
                raise NeedsConversionError("non_numeric_embedding") from e

        return np.ascontiguousarray(self._read(read), dtype="<f4")

    def column_meta(self, name: str) -> ColumnMeta:
        col = next(c for c in self.info.obs.columns if c.name == name)
        meta = ColumnMeta(name=name, kind=col.kind, dtype=col.dtype, ordered=col.ordered)
        if col.kind == "boolean":
            meta.categories, meta.n_categories = ["false", "true"], 2
            colors = self._read(lambda: self._uns_colors(name))
            if colors is not None:  # scanpy plots booleans as two categories (False, True)
                meta.colors = valid_colors(colors, 2, name)
        if col.kind != "categorical":
            # Plain string columns: labels come from /obs/labels (they need a full read).
            return meta
        meta.n_categories = col.n_categories
        if (col.n_categories or 0) > MAX_CATEGORIES:
            return meta

        def read() -> tuple[Any, Any]:
            node = self.group["obs"][name]
            cats = read_elem(node["categories"]) if "categories" in node else None
            return cats, self._uns_colors(name)

        cats, colors = self._read(read)
        if cats is not None:
            meta.categories = [str(c) for c in np.asarray(cats)]
            meta.n_categories = len(meta.categories)
            meta.colors = (
                valid_colors(colors, meta.n_categories, name) if colors is not None else None
            )
        return meta

    def var(self) -> pd.DataFrame:
        return self._read(lambda: read_elem(self.group["var"]))

    def _matrix_node(self, matrix: str) -> Any:
        return self.group["X"] if matrix == "X" else self.group["layers"][matrix]

    def gene(self, matrix: str, i: int) -> GeneValues:
        m = _check_gene(self.info, matrix, i)
        n = self.info.n_obs

        def read() -> GeneValues:
            node = self._matrix_node(matrix)
            if m.encoding == "dense":
                return from_dense(node[:, i])
            if m.encoding != "csc":
                raise NeedsConversionError("matrix_csr")
            a, b = (int(x) for x in node["indptr"][i : i + 2])
            return from_column(node["indices"][a:b], node["data"][a:b], n)

        return self._read(read)  # type: ignore[no-any-return]

    def matrix_columns(self, matrix: str) -> Any:
        m = _check_gene(self.info, matrix, 0)
        n_obs, n_vars = m.shape
        if m.encoding == "dense":
            out = np.empty((n_obs, n_vars), dtype=np.float32, order="F")
            rows = max(1, BLOCK_VALUES // max(n_vars, 1))
            for a in range(0, n_obs, rows):
                b = min(a + rows, n_obs)
                # one block per lock hold: other requests on this dataset get turns in between
                out[a:b] = self._read(lambda a=a, b=b: self._matrix_node(matrix)[a:b, :])
            return out
        if m.encoding == "csc":
            return _to_columns(self._read(lambda: read_elem(self._matrix_node(matrix))))
        indptr = self._read(lambda: np.asarray(self._matrix_node(matrix)["indptr"][:]))

        def read(lo: int, hi: int, *, values: bool) -> tuple[Any, Any]:
            def get() -> tuple[Any, Any]:
                node = self._matrix_node(matrix)
                return node["indices"][lo:hi], (node["data"][lo:hi] if values else None)

            return self._read(get)  # type: ignore[no-any-return]

        return csr_to_csc_blocks(indptr, n_vars, read)

    def uns_colors(self, name: str) -> Any:
        return self._read(lambda: self._uns_colors(name))

    def stored_subset(self, n: int) -> np.ndarray | None:
        def read() -> np.ndarray | None:
            try:
                node = self.group["uns"]["canopy"]["downsample"][str(n)]
            except KeyError:
                return None
            return np.asarray(node[:])

        return self._read(read)  # type: ignore[no-any-return]

    def _uns_colors(self, name: str) -> Any:
        uns = self.group.get("uns")
        key = f"{name}_colors"
        if uns is None or key not in uns:
            return None
        return read_elem(uns[key])

    def _read(self, fn: Any) -> Any:
        with self.lock:
            if self.closed:
                raise SourceClosedError
            return fn()

    def close(self) -> None:
        self.closed = True
        if self.handle is not None and hasattr(self.handle, "close"):
            self.handle.close()


class MemorySource(Source):
    mode = "memory"

    def __init__(self, info: AnnDataInfo, adata: Any) -> None:
        super().__init__(info)
        self.adata = adata

    def obs(self, name: str) -> ObsValues:
        if name not in self.adata.obs.columns:
            raise UnknownElementError("column")
        return to_obs_values(self.adata.obs[name])

    def obsm(self, key: str, dims: list[int]) -> np.ndarray:
        _check_dims(self.info, key, dims)
        arr = self.adata.obsm[key]
        arr = arr.to_numpy() if hasattr(arr, "to_numpy") else np.asarray(arr)
        return np.ascontiguousarray(arr[:, dims], dtype="<f4")

    def column_meta(self, name: str) -> ColumnMeta:
        col = next(c for c in self.info.obs.columns if c.name == name)
        meta = ColumnMeta(name=name, kind=col.kind, dtype=col.dtype, ordered=col.ordered)
        if col.kind == "boolean":
            meta.categories, meta.n_categories = ["false", "true"], 2
            colors = self.adata.uns.get(f"{name}_colors")
            if colors is not None:
                meta.colors = valid_colors(colors, 2, name)
        s = self.adata.obs.get(name)
        if col.kind == "categorical" and s is not None and hasattr(s, "cat"):
            meta.n_categories = len(s.cat.categories)
            if meta.n_categories <= MAX_CATEGORIES:
                meta.categories = [str(c) for c in s.cat.categories]
                colors = self.adata.uns.get(f"{name}_colors")
                meta.colors = (
                    valid_colors(colors, meta.n_categories, name) if colors is not None else None
                )
        return meta

    def var(self) -> pd.DataFrame:
        return self.adata.var

    def uns_colors(self, name: str) -> Any:
        return self.adata.uns.get(f"{name}_colors")

    def _matrix(self, matrix: str) -> Any:
        return self.adata.X if matrix == "X" else self.adata.layers[matrix]

    def gene(self, matrix: str, i: int) -> GeneValues:
        _check_gene(self.info, matrix, i)
        mat = self._matrix(matrix)
        if sparse.issparse(mat):
            if mat.format != "csc":
                raise NeedsConversionError("matrix_csr")
            a, b = int(mat.indptr[i]), int(mat.indptr[i + 1])
            return from_column(mat.indices[a:b], mat.data[a:b], mat.shape[0])
        return from_dense(np.asarray(mat[:, i]))

    def matrix_columns(self, matrix: str) -> Any:
        _check_gene(self.info, matrix, 0)
        return _to_columns(self._matrix(matrix))


def _to_columns(mat: Any) -> Any:
    """A loaded matrix as float32 CSC (sparse) or column-major float32 ndarray (dense)."""
    if sparse.issparse(mat):
        return sparse.csc_matrix(mat, dtype=np.float32)
    return np.asfortranarray(np.asarray(mat, dtype=np.float32))
