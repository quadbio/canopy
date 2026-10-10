"""Describe an AnnData object stored in h5ad (h5py) or zarr, from metadata only."""

from __future__ import annotations

from dbx.inspect.models import (
    AnnDataInfo,
    AnnDataLayout,
    ArrayInfo,
    ColumnInfo,
    FrameInfo,
    MatrixInfo,
)
from dbx.inspect.nodes import Node, attr_list

_SPARSE = {"csr_matrix": "csr", "csc_matrix": "csc"}


def matrix_info(node: Node) -> MatrixInfo | None:
    """Dense array or anndata sparse group; None for anything else."""
    if not node.is_group:
        return MatrixInfo(
            encoding="dense", shape=node.shape, dtype=node.dtype, storage=node.array_info()
        )
    attrs = node.attrs
    enc = _SPARSE.get(node.encoding_type or "")
    shape_attr = attrs.get("shape")
    if enc is None and attrs.get("h5sparse_format") in ("csr", "csc"):  # anndata < 0.7
        enc = attrs["h5sparse_format"]
        shape_attr = attrs.get("h5sparse_shape")
    if enc is None or "data" not in node:
        return None
    data = node["data"]
    shape = [int(x) for x in shape_attr or []]
    return MatrixInfo(
        encoding=enc,  # type: ignore[arg-type]
        shape=shape,
        dtype=data.dtype,
        nnz=data.shape[0],
        storage=data.array_info(),
        indices_dtype=node["indices"].dtype if "indices" in node else None,
        indptr_dtype=node["indptr"].dtype if "indptr" in node else None,
    )


def _plain_kind(dtype: str, kind_letter: str) -> str:
    if dtype == "bool":
        return "boolean"
    if kind_letter in "SUO" or dtype in ("object", "str", "string"):
        return "string"
    return "numeric"


def _column(
    name: str, node: Node, colors: dict[str, int], old_categories: Node | None = None
) -> ColumnInfo:
    et = node.encoding_type
    if old_categories is not None and not node.is_group:  # anndata 0.7 categorical
        return ColumnInfo(
            name=name,
            kind="categorical",
            dtype="string" if old_categories.dtype_kind in "SUO" else old_categories.dtype,
            n_categories=old_categories.length,
            ordered=bool(node.attrs.get("ordered", False)),
            has_colors=name in colors,
        )
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
        kind = _plain_kind(node.dtype, node.dtype_kind)
        dtype = "string" if kind == "string" else node.dtype
        return ColumnInfo(name=name, kind=kind, dtype=dtype)  # type: ignore[arg-type]
    return ColumnInfo(name=name, kind="other", dtype=et or "group")


LEGACY_INDEX_FIELDS = ("index", "_index")


def _compound_frame(node: Node, colors: dict[str, int], uns: Node | None) -> FrameInfo:
    """anndata < 0.7: the frame is one compound dataset; categorical codes take their categories
    from uns/<col>_categories."""
    fields = node.fields()
    index_name = next((f for f in LEGACY_INDEX_FIELDS if f in fields), None)
    cols: list[ColumnInfo] = []
    for name, (dtype, _sub) in fields.items():
        if name == index_name:
            continue
        cat_key = f"{name}_categories"
        if uns is not None and cat_key in uns:
            cols.append(
                ColumnInfo(
                    name=name,
                    kind="categorical",
                    dtype="string",
                    n_categories=uns[cat_key].length,
                    has_colors=name in colors,
                )
            )
        else:
            kind = "boolean" if dtype == "bool" else "string" if dtype == "string" else "numeric"
            cols.append(ColumnInfo(name=name, kind=kind, dtype=dtype))  # type: ignore[arg-type]
    return FrameInfo(n_rows=node.length, index_name=None, columns=cols)


def frame_info(node: Node, colors: dict[str, int], uns: Node | None = None) -> FrameInfo:
    if not node.is_group:
        return _compound_frame(node, colors, uns)
    attrs = node.attrs
    index_key = attrs.get("_index", "_index")
    n_rows = node[index_key].length if index_key in node else 0
    old_cats = node.get("__categories")
    order = attr_list(attrs, "column-order") or [
        k for k in node if k not in (index_key, "__categories")
    ]
    cols = [
        _column(c, node[c], colors, old_cats[c] if old_cats is not None and c in old_cats else None)
        for c in order
        if c in node
    ]
    index_name = index_key if index_key not in ("_index", "index") else None
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
    if not node.is_group:  # anndata < 0.7: obsm/varm as one compound dataset
        n = node.length
        return {
            k: ArrayInfo(shape=[n, *(sub or [1])], dtype=dtype)
            for k, (dtype, sub) in node.fields().items()
        }
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


def _layout(root: Node) -> AnnDataLayout:
    obs = root.get("obs")
    if obs is not None and not obs.is_group:
        return "pre-0.7"
    if obs is not None and "__categories" in obs:
        return "0.7"
    return "current"


def anndata_info(root: Node) -> AnnDataInfo:
    uns = root.get("uns")
    colors = _color_lengths(uns)
    layout = _layout(root)
    obs = frame_info(root["obs"], colors, uns)
    var = frame_info(root["var"], {}, uns)
    x = matrix_info(root["X"]) if "X" in root else None
    raw = root.get("raw")
    raw_x_node = raw.get("X") if raw is not None else root.get("raw.X")
    raw_var_node = raw.get("var") if raw is not None else root.get("raw.var")
    raw_x = matrix_info(raw_x_node) if raw_x_node is not None else None
    raw_vars = frame_info(raw_var_node, {}).n_rows if raw_var_node is not None else None
    if layout == "pre-0.7" and uns is not None:
        # uns/<col>_categories belong to obs/var columns, not to uns proper.
        cat_keys = {f"{c.name}_categories" for c in obs.columns + var.columns}
        uns_keys = [k for k in uns if k not in cat_keys]
    else:
        uns_keys = uns.keys() if uns is not None else []
    return AnnDataInfo(
        encoding_version=root.attrs.get("encoding-version"),
        layout=layout,
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
        uns_keys=uns_keys,
        colors=colors,
    )
