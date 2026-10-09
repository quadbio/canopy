"""Describe an AnnData object stored in h5ad (h5py) or zarr, from metadata only."""

from __future__ import annotations

from dbx.inspect.models import AnnDataInfo, ArrayInfo, ColumnInfo, FrameInfo, MatrixInfo
from dbx.inspect.nodes import Node, attr_list

_SPARSE = {"csr_matrix": "csr", "csc_matrix": "csc"}


def matrix_info(node: Node) -> MatrixInfo | None:
    """Dense array or anndata sparse group; None for anything else."""
    if not node.is_group:
        return MatrixInfo(
            encoding="dense", shape=node.shape, dtype=node.dtype, storage=node.array_info()
        )
    enc = _SPARSE.get(node.encoding_type or "")
    if enc is None or "data" not in node:
        return None
    data = node["data"]
    shape = [int(x) for x in node.attrs.get("shape", [])]
    return MatrixInfo(
        encoding=enc,  # type: ignore[arg-type]
        shape=shape,
        dtype=data.dtype,
        nnz=data.shape[0],
        storage=data.array_info(),
        indices_dtype=node["indices"].dtype if "indices" in node else None,
        indptr_dtype=node["indptr"].dtype if "indptr" in node else None,
    )


def _column(name: str, node: Node, colors: dict[str, int]) -> ColumnInfo:
    et = node.encoding_type
    if et == "categorical":
        cats = node["categories"]
        return ColumnInfo(
            name=name,
            kind="categorical",
            dtype=cats.dtype,
            n_categories=cats.shape[0] if cats.shape else 0,
            ordered=bool(node.attrs.get("ordered", False)),
            has_colors=name in colors,
        )
    if et in ("string-array", "nullable-string-array"):
        return ColumnInfo(name=name, kind="string", dtype="string")
    if et == "nullable-boolean":
        return ColumnInfo(name=name, kind="boolean", dtype="bool")
    if et == "nullable-integer":
        return ColumnInfo(name=name, kind="numeric", dtype=node["values"].dtype)
    if not node.is_group:
        dt = node.dtype
        if dt == "bool":
            kind = "boolean"
        elif dt in ("object", "str") or dt.startswith(("<U", "|S", "S", "U")):
            kind = "string"
        else:
            kind = "numeric"
        return ColumnInfo(name=name, kind=kind, dtype=dt)  # type: ignore[arg-type]
    return ColumnInfo(name=name, kind="other", dtype=et or "group")


def frame_info(node: Node, colors: dict[str, int]) -> FrameInfo:
    attrs = node.attrs
    index_key = attrs.get("_index", "_index")
    n_rows = node[index_key].length if index_key in node else 0
    order = attr_list(attrs, "column-order") or [k for k in node if k != index_key]
    cols = [_column(c, node[c], colors) for c in order if c in node]
    index_name = index_key if index_key != "_index" else None
    return FrameInfo(n_rows=n_rows, index_name=index_name, columns=cols)


def _color_lengths(uns: Node | None) -> dict[str, int]:
    if uns is None:
        return {}
    out: dict[str, int] = {}
    for key, child in uns.items():
        if key.endswith("_colors") and not child.is_group:
            out[key[: -len("_colors")]] = child.shape[0] if child.shape else 0
    return out


def _arrays(node: Node | None) -> dict[str, ArrayInfo]:
    if node is None:
        return {}
    out: dict[str, ArrayInfo] = {}
    for k, child in node.items():
        if not child.is_group:
            out[k] = child.array_info()
        elif child.encoding_type == "dataframe":
            # e.g. obsm stored as a dataframe; record its row count and width
            fi = frame_info(child, {})
            out[k] = ArrayInfo(shape=[fi.n_rows, len(fi.columns)], dtype="dataframe")
    return out


def _matrices(node: Node | None) -> dict[str, MatrixInfo]:
    if node is None:
        return {}
    out: dict[str, MatrixInfo] = {}
    for k, child in node.items():
        mi = matrix_info(child)
        if mi is not None:
            out[k] = mi
    return out


def anndata_info(root: Node) -> AnnDataInfo:
    uns = root.get("uns")
    colors = _color_lengths(uns)
    obs = frame_info(root["obs"], colors)
    var = frame_info(root["var"], {})
    x = matrix_info(root["X"]) if "X" in root else None
    raw = root.get("raw")
    raw_x = matrix_info(raw["X"]) if raw is not None and "X" in raw else None
    raw_vars = frame_info(raw["var"], {}).n_rows if raw is not None and "var" in raw else None
    return AnnDataInfo(
        encoding_version=root.attrs.get("encoding-version"),
        n_obs=obs.n_rows,
        n_vars=var.n_rows,
        X=x,
        layers=_matrices(root.get("layers")),
        raw=raw_x,
        raw_n_vars=raw_vars,
        obs=obs,
        var=var,
        obsm=_arrays(root.get("obsm")),
        varm=_arrays(root.get("varm")),
        obsp=_matrices(root.get("obsp")),
        uns_keys=uns.keys() if uns is not None else [],
        colors=colors,
    )
