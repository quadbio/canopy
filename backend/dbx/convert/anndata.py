"""Convert an h5ad or AnnData-zarr file into a dbx store (contract/SPEC.md §2–§4).

The store is built in `<cache>/<version>/c0.tmp-<random>/`, validated, and only then renamed to
`c0/`, so a store that exists is always complete. Memory stays within the job's budget: obs and
var are read whole (they are small next to X), embeddings and X in blocks.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import zarr
from anndata.io import read_elem

import dbx
from dbx.contract import MANIFEST, validate_store
from dbx.contract.models import (
    CONTRACT_MAJOR,
    CONTRACT_VERSION,
    DOWNSAMPLE_SIZES,
    DOWNSAMPLE_THRESHOLD,
    OBSM_CHUNK,
    CategoricalInfo,
    ConverterInfo,
    Manifest,
    SourceInfo,
    TableEntry,
)
from dbx.contract.write import ANNDATA_ARRAY, anndata_dict_group, array, codecs
from dbx.convert.matrix import ConversionError, CscWriter, GeneStats
from dbx.data.genes import gene_list
from dbx.data.reader import embedding_dims, parse_colors
from dbx.inspect.models import AnnDataInfo, Fingerprint
from dbx.logs import get_logger, timed
from dbx.synth.anndata_gen import consolidate

Progress = Callable[[str, float], None]
TABLE = "table"  # a plain AnnData file has one table, named "table" (SPEC §2)

# Overall progress: share of the work each stage takes (roughly, for a CSR source).
STAGES = {"read": 0.05, "obsm": 0.05, "count": 0.15, "reorder": 0.4, "write": 0.25, "finish": 0.1}


@dataclass
class ConvertOptions:
    # Which source matrix becomes X: "X" (default), "layers/<name>" or "raw/X" (SPEC §4.2).
    x_source: str = "X"
    memory_bytes: int = 2 * 2**30
    tmp_dir: Path | None = None


@dataclass
class Result:
    store: Path
    manifest: Manifest
    warnings: list[str] = field(default_factory=list)


def store_dir(cache_dir: Path, version: str) -> Path:
    return cache_dir / version / f"c{CONTRACT_MAJOR}"


def has_store(cache_dir: Path, version: str) -> bool:
    return (store_dir(cache_dir, version) / MANIFEST).exists()


class StageProgress:
    """Maps per-stage fractions to one overall fraction (STAGES weights)."""

    def __init__(self, report: Progress) -> None:
        self.report = report
        self.done = 0.0

    def __call__(self, stage: str, fraction: float) -> None:
        names = list(STAGES)
        before = sum(STAGES[n] for n in names[: names.index(stage)]) if stage in STAGES else 0.0
        self.report(stage, min(1.0, before + STAGES.get(stage, 0.0) * fraction))


def _open(path: Path, kind: str) -> tuple[Any, Any]:
    """The AnnData root group and the handle to close."""
    if kind == "h5ad":
        f = h5py.File(path, "r")
        return f, f
    return zarr.open_group(path, mode="r"), None


def _matrix_node(root: Any, info: AnnDataInfo, x_source: str) -> tuple[Any, str, list[int]]:
    """The source matrix chosen as X, its encoding and shape."""
    if x_source == "X":
        m, node = info.X, root.get("X")
        if m is None and info.raw is not None:  # SPEC §4.2: raw/X when X is missing
            return _matrix_node(root, info, "raw/X")
    elif x_source.startswith("layers/"):
        name = x_source.split("/", 1)[1]
        m, node = info.layers.get(name), root.get("layers", {}).get(name)
    elif x_source == "raw/X":
        m, node = info.raw, root.get("raw", {}).get("X")
    else:
        raise ConversionError("unknown_matrix", f"no matrix {x_source}")
    if m is None or node is None:
        raise ConversionError("unknown_matrix", f"no matrix {x_source}")
    if len(m.shape) != 2 or m.shape[0] != info.n_obs:
        raise ConversionError("matrix_shape", "the matrix does not have one row per cell")
    return node, m.encoding, list(m.shape)


def convert_anndata(
    src: Path,
    fp: Fingerprint,
    cache_dir: Path,
    opts: ConvertOptions,
    progress: Progress,
) -> Result:
    if fp.anndata is None or fp.kind not in ("h5ad", "anndata_zarr"):
        raise ConversionError("not_anndata", "only h5ad and AnnData-zarr files are converted here")
    info = fp.anndata
    if info.layout != "current":
        raise ConversionError("legacy_layout", "files written by anndata < 0.8 are not supported")
    log = get_logger("dbx.convert")
    t0 = time.monotonic()
    final = store_dir(cache_dir, fp.dataset_id)
    tmp = final.parent / f"{final.name}.tmp-{uuid.uuid4().hex[:8]}"
    work = (opts.tmp_dir or cache_dir / "tmp") / tmp.name
    prog = StageProgress(progress)
    warnings: list[str] = []
    root, handle = _open(src, fp.kind)
    try:
        tmp.mkdir(parents=True)
        store = zarr.open_group(tmp, mode="w")
        store.attrs["dbx"] = {"contract_version": CONTRACT_VERSION}
        with timed("convert/read_obs_var", dataset_id=fp.dataset_id) as extra:
            obs: pd.DataFrame = read_elem(root["obs"])
            var: pd.DataFrame = read_elem(root["var"])
            extra.update(n_obs=len(obs), n_vars=len(var), obs_columns=len(obs.columns))
        node, encoding, shape = _matrix_node(root, info, opts.x_source)
        if shape[1] != len(var):
            if opts.x_source == "raw/X" or (opts.x_source == "X" and info.X is None):
                var = read_elem(root["raw"]["var"])
            else:
                raise ConversionError(
                    "matrix_shape", "the matrix does not have one column per gene"
                )
        uns = _colours(root, obs, warnings)
        prog("read", 1.0)

        # ---- the AnnData skeleton: obs, var, colours (X and obsm are written below)
        tpath = tmp / "tables" / TABLE
        store.require_group("tables")
        ad.AnnData(obs=obs, var=var, uns=uns).write_zarr(tpath)
        tg = zarr.open_group(tpath, mode="a", use_consolidated=False)
        embeddings, spatial = _obsm(root, info, tg, warnings, lambda f: prog("obsm", f))

        # ---- X as gene-major CSC, with gene statistics
        with timed("convert/x", dataset_id=fp.dataset_id, encoding=encoding) as extra:
            writer = CscWriter(node, encoding, (shape[0], shape[1]), opts.memory_bytes, work, prog)
            if "X" in tg:
                del tg["X"]
            stats = writer.write(tg.create_group("X"))
            nnz = writer.nnz
            extra.update(nnz=nnz)
        warnings += stats.warnings
        dbxg = anndata_dict_group(tg["uns"], "dbx")  # type: ignore[arg-type]
        _gene_stats(dbxg, stats)
        sizes = _downsample(dbxg, obs, uns)
        prog("finish", 0.3)

        # ---- manifest, consolidation, validation, publish
        consolidate(tpath)
        consolidate(tmp)
        symbols = gene_list(var).symbol_column
        manifest = Manifest(
            dataset_id=fp.dataset_id,
            source=SourceInfo(
                path=src.name,
                kind=fp.kind,
                size_bytes=int(fp.size_bytes or 0),
                mtime=float(fp.mtime or 0.0),
                fingerprint_version=fp.fingerprint_version,
            ),
            converter=ConverterInfo(
                dbx_version=dbx.__version__,
                created=datetime.now(UTC).isoformat(timespec="seconds"),
                duration_s=round(time.monotonic() - t0, 1),
                warnings=warnings,
            ),
            tables={
                TABLE: TableEntry(
                    path=f"tables/{TABLE}",
                    n_obs=shape[0],
                    n_vars=shape[1],
                    nnz=nnz,
                    x_source=opts.x_source if opts.x_source != "X" or info.X else "raw/X",
                    embeddings=embeddings,
                    spatial_keys=spatial,
                    categorical={
                        c: CategoricalInfo(
                            n_categories=len(obs[c].cat.categories),
                            has_colors=f"{c}_colors" in uns,
                        )
                        for c in obs.columns
                        if isinstance(obs[c].dtype, pd.CategoricalDtype)
                    },
                    numeric=[
                        c
                        for c in obs.columns
                        if pd.api.types.is_numeric_dtype(obs[c].dtype)
                        and not pd.api.types.is_bool_dtype(obs[c].dtype)
                    ],
                    var_names_column=symbols,
                    downsample=sizes,
                ),
            },
        )
        (tmp / MANIFEST).write_text(json.dumps(manifest.model_dump(mode="json"), indent=2))
        with timed("convert/validate", dataset_id=fp.dataset_id) as extra:
            report = validate_store(tmp, deep=True)
            extra.update(errors=len(report.errors), warnings=len(report.warnings))
        if not report.ok:
            log.error("store_invalid", dataset_id=fp.dataset_id, errors=report.errors[:20])
            raise ConversionError("store_invalid", "the converted store failed validation")
        prog("finish", 0.9)
        if final.exists():
            shutil.rmtree(final)
        tmp.rename(final)
        prog("finish", 1.0)
        return Result(final, manifest, warnings)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if handle is not None:
            handle.close()


def _colours(root: Any, obs: pd.DataFrame, warnings: list[str]) -> dict[str, Any]:
    """`uns['<col>_colors']` for categorical and text columns, as `#rrggbb` (same rules as the
    preview: names converted, longer lists cut, unusable lists dropped with a warning)."""
    out: dict[str, Any] = {}
    uns = root.get("uns")
    if uns is None:
        return out
    for col in obs.columns:
        key = f"{col}_colors"
        if key not in uns:
            continue
        s = obs[col]
        if isinstance(s.dtype, pd.CategoricalDtype):
            n = len(s.cat.categories)
        elif s.dtype == object or pd.api.types.is_string_dtype(s.dtype):
            n = int(s.dropna().nunique())
        else:
            continue
        cols, why = parse_colors(read_elem(uns[key]), n)
        if cols is None:
            warnings.append(f"colours_dropped:{why}")
        else:
            out[key] = np.asarray(cols, dtype=object)
    return out


def _obsm(
    root: Any,
    info: AnnDataInfo,
    tg: zarr.Group,
    warnings: list[str],
    progress: Callable[[float], None],
) -> tuple[dict[str, int], list[str]]:
    """Every numeric embedding with ≥ 2 columns as float32, chunked by OBSM_CHUNK rows."""
    keys = embedding_dims(info)
    obsm = anndata_dict_group(tg, "obsm")
    for name in list(obsm.keys()):
        del obsm[name]
    out: dict[str, int] = {}
    for k, (key, dims) in enumerate(keys.items()):
        node = root["obsm"][key]
        if not hasattr(node, "shape"):  # a data frame
            warnings.append("obsm_frame_skipped")
            continue
        a = obsm.create_array(
            key,
            shape=(info.n_obs, dims),
            dtype=np.float32,
            chunks=(OBSM_CHUNK, dims),
            compressors=codecs(),
        )
        a.attrs.update(ANNDATA_ARRAY)
        for s in range(0, info.n_obs, OBSM_CHUNK * 4):
            e = min(s + OBSM_CHUNK * 4, info.n_obs)
            a[s:e] = np.asarray(node[s:e], dtype=np.float32)
        out[key] = dims
        progress((k + 1) / max(len(keys), 1))
    skipped = set(info.obsm) - set(keys)
    if skipped:
        warnings.append("obsm_not_embedding_skipped")
    spatial = [k for k in out if k == "spatial" or k.startswith("spatial")]
    return {k: d for k, d in out.items() if k not in spatial}, spatial


def _gene_stats(dbxg: zarr.Group, stats: GeneStats) -> None:
    g = anndata_dict_group(dbxg, "gene_stats")
    for name in ("nnz", "mean", "max", "p99"):
        array(g, name, np.asarray(getattr(stats, name), dtype=np.float32), attrs=ANNDATA_ARRAY)


def downsample_indices(n_obs: int, n: int, groups: np.ndarray | None, seed: int = 0) -> np.ndarray:
    """`n` sorted cell indices; stratified by `groups` (codes, -1 = none) so every group keeps
    at least one cell, proportionally otherwise; uniform without groups."""
    rng = np.random.default_rng(seed)
    if groups is None:
        return np.sort(rng.choice(n_obs, size=n, replace=False))
    codes = np.asarray(groups, dtype=np.int64) + 1  # missing → group 0
    counts = np.bincount(codes)
    present = counts > 0
    want = counts * (n / n_obs)
    quota = np.floor(want).astype(np.int64)
    quota[present & (quota == 0)] = 1
    quota = np.minimum(quota, counts)
    short = n - int(quota.sum())
    if short > 0:  # largest remainders first, within each group's size
        room = counts - quota
        order = np.argsort(-(want - np.floor(want)), kind="stable")
        for g in order:
            if short == 0:
                break
            take = int(min(room[g], short))
            quota[g] += take
            short -= take
    elif short < 0:  # the "at least one" rule overshot: take back from the largest
        for g in np.argsort(-quota):
            give = int(min(quota[g] - 1, -short))
            quota[g] -= give
            short += give
            if short == 0:
                break
    order = np.argsort(codes, kind="stable")
    bounds = np.concatenate([[0], np.cumsum(counts)])
    picked = [
        rng.choice(order[bounds[g] : bounds[g + 1]], size=int(quota[g]), replace=False)
        for g in range(len(counts))
        if quota[g]
    ]
    return np.sort(np.concatenate(picked)) if picked else np.zeros(0, np.int64)


def _downsample(dbxg: zarr.Group, obs: pd.DataFrame, uns: dict[str, Any]) -> list[int]:
    n_obs = len(obs)
    if n_obs <= DOWNSAMPLE_THRESHOLD:
        return []
    by = next(
        (
            c
            for c in obs.columns
            if isinstance(obs[c].dtype, pd.CategoricalDtype) and f"{c}_colors" in uns
        ),
        None,
    )
    groups = obs[by].cat.codes.to_numpy() if by else None
    g = anndata_dict_group(dbxg, "downsample")
    dtype = np.int32 if n_obs < 2**31 else np.int64
    sizes = [n for n in DOWNSAMPLE_SIZES if n < n_obs]
    for n in sizes:
        idx = downsample_indices(n_obs, n, groups).astype(dtype)
        array(g, str(n), idx, attrs=ANNDATA_ARRAY)
    return sizes
