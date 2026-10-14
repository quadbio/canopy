"""Convert an h5ad or AnnData-zarr file into a canopy store (contract/SPEC.md §2–§4).

The store is built in `<cache>/<version>/c0.tmp-<random>/`, validated, and only then renamed to
`c0/`, so a store that exists is always complete. Memory stays within the job's budget: obs and
var are read whole (they are small next to X), embeddings and X in blocks.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anndata as ad
import numpy as np
import pandas as pd
import zarr
from anndata.io import read_elem

import canopy
from canopy.contract import MANIFEST, validate_store
from canopy.contract.models import (
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
from canopy.contract.write import ANNDATA_ARRAY, anndata_dict_group, array, codecs, consolidate
from canopy.convert.matrix import ConversionError, CscWriter, GeneStats
from canopy.data.genes import gene_list
from canopy.data.reader import embedding_dims, parse_colors
from canopy.inspect.core import dataset_id, open_h5, version_stamp
from canopy.inspect.guard import open_zarr
from canopy.inspect.models import AnnDataInfo, Fingerprint, MatrixInfo
from canopy.logs import get_logger, timed

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
    job: int = 0  # job id, in the temporary folders' names (`c0.tmp-j<job>-…`)
    # SpatialData: the points column holding the genes, when it cannot be inferred.
    feature_key: str | None = None
    # Threads for block-wise work (image and mask pyramids); bounded on shared hosts.
    threads: int = 4


@dataclass
class Result:
    store: Path
    manifest: Manifest
    warnings: list[str] = field(default_factory=list)


def table_memory_estimate(info: AnnDataInfo) -> int:
    """Rough memory of obs and var read whole (they are outside the matrix budget): a pandas
    index of strings plus the columns (text values counted at 64 bytes each)."""

    def frame(n: int, cols: list[Any]) -> int:
        # measured (review A-m2, 2 M cells): an index string ≈120 B, a text value ≈150 B
        per = 120
        for c in cols:
            per += {"categorical": 4, "numeric": 8, "boolean": 1}.get(c.kind, 150)
        return n * per

    return frame(info.n_obs, info.obs.columns) + frame(info.n_vars, info.var.columns)


def job_memory_estimate(fp: Fingerprint, threads: int = 4) -> int:
    """Memory a conversion reads whole besides the matrix budget (for the memory guard)."""
    if fp.anndata is not None:
        return table_memory_estimate(fp.anndata)
    if fp.spatialdata is not None:
        from canopy.convert.spatialdata import spatial_memory_estimate

        return spatial_memory_estimate(fp, threads)
    return 0


def store_dir(cache_dir: Path, version: str) -> Path:
    return cache_dir / version / f"c{CONTRACT_MAJOR}"


def has_store(cache_dir: Path, version: str) -> bool:
    """A complete store of this contract (a store written under another name, e.g. before the
    project became Canopy, counts as absent and is converted again)."""
    from canopy.contract.write import is_store

    return is_store(store_dir(cache_dir, version))


class StageProgress:
    """Maps per-stage fractions to one overall fraction (`stages` weights)."""

    def __init__(self, report: Progress, stages: dict[str, float] = STAGES) -> None:
        self.report = report
        self.stages = stages
        self.done = 0.0

    def __call__(self, stage: str, fraction: float) -> None:
        st = self.stages
        names = list(st)
        before = sum(st[n] for n in names[: names.index(stage)]) if stage in st else 0.0
        self.report(stage, min(1.0, before + st.get(stage, 0.0) * fraction))


def _open(path: Path, kind: str) -> tuple[Any, Any]:
    """The AnnData root group and the handle to close."""
    if kind == "h5ad":
        f = open_h5(path)  # lock and truncation errors with recognisable codes
        return f, f
    return open_zarr(path), None


def _matrix_node(root: Any, info: AnnDataInfo, x_source: str) -> tuple[Any, str, list[int], bool]:
    """The source matrix chosen as X, its encoding and shape, and whether it is raw/X."""
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
    return node, m.encoding, list(m.shape), x_source == "raw/X"


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
    log = get_logger("canopy.convert")
    t0 = time.monotonic()
    version = fp.dataset_id
    stamp = version_stamp(src)
    if dataset_id(src, stamp, fp.identity) != version:
        raise ConversionError("source_changed", "the file changed since it was inspected")
    final = store_dir(cache_dir, version)
    tmp = final.parent / f"{final.name}.tmp-j{opts.job}-{uuid.uuid4().hex[:8]}"
    spill_root = opts.tmp_dir or cache_dir / "tmp"
    work = spill_root / tmp.name
    prog = StageProgress(progress)
    warnings: list[str] = []
    node_info = _matrix_info(info, opts.x_source)
    _check_disk(final.parent, spill_root, node_info, version, log)
    root, handle = _open(src, fp.kind)
    try:
        tmp.mkdir(parents=True)
        store = zarr.open_group(tmp, mode="w")
        store.attrs["canopy"] = {"contract_version": CONTRACT_VERSION}
        store.require_group("tables")
        entry = write_table(root, info, tmp, TABLE, opts, work, prog, warnings, version)
        prog("finish", 0.3)

        # ---- manifest, consolidation, validation, publish
        consolidate(tmp)
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
                canopy_version=canopy.__version__,
                created=datetime.now(UTC).isoformat(timespec="seconds"),
                duration_s=round(time.monotonic() - t0, 1),
                peak_rss_gb=peak_rss_gb(),
                warnings=warnings,
            ),
            tables={TABLE: entry},
        )
        write_manifest(tmp, manifest)
        with timed("convert/validate", version=version) as extra:
            report = validate_store(tmp, deep=True)
            extra.update(errors=len(report.errors), warnings=len(report.warnings))
        if not report.ok:
            log_invalid(log, version, report)
        if version_stamp(src) != stamp:
            raise ConversionError("source_changed", "the file changed during the conversion")
        prog("finish", 0.9)
        _publish(tmp, final)
        prog("finish", 1.0)
        return Result(final, manifest, warnings)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if handle is not None:
            handle.close()


def write_table(
    root: Any,
    info: AnnDataInfo,
    store: Path,
    name: str,
    opts: ConvertOptions,
    work: Path,
    prog: StageProgress,
    warnings: list[str],
    version: str,
    region: dict[str, Any] | None = None,
) -> TableEntry:
    """Write one AnnData table (`root`: its group in the source) to `store/tables/<name>`:
    obs, var, colours, embeddings, X as gene-major CSC with gene statistics, and subsets.
    `region`: SpatialData's region/region_key/instance_key for the manifest."""
    node, encoding, shape, from_raw = _matrix_node(root, info, opts.x_source)
    with timed("convert/read_obs_var", version=version, table=name) as extra:
        obs: pd.DataFrame = read_elem(root["obs"])
        # raw/X has its own genes: always raw's var, also when the counts happen to match
        var: pd.DataFrame = read_elem(root["raw"]["var"] if from_raw else root["var"])
        extra.update(
            n_obs=len(obs),
            n_vars=len(var),
            obs_columns=len(obs.columns),
            estimate_bytes=table_memory_estimate(info),
        )
    if shape[1] != len(var):
        raise ConversionError("matrix_shape", "the matrix does not have one column per gene")
    uns = _colours(root, obs, warnings)
    prog("read", 1.0)

    # ---- the AnnData skeleton: obs, var, colours (X and obsm are written below)
    tpath = store / "tables" / name
    with timed("convert/write_obs_var", version=version, table=name):
        # strings stay strings ("values are never changed, only re-laid-out")
        ad.AnnData(obs=obs, var=var, uns=uns).write_zarr(
            tpath, convert_strings_to_categoricals=False
        )
    tg = zarr.open_group(tpath, mode="a", use_consolidated=False)
    with timed("convert/obsm", version=version, table=name) as extra:
        embeddings, spatial = _obsm(root, info, tg, warnings, lambda f: prog("obsm", f))
        extra.update(n_embeddings=len(embeddings) + len(spatial))

    # ---- X as gene-major CSC, with gene statistics
    node_info = _matrix_info(info, opts.x_source)
    with timed(
        "convert/x", version=version, table=name, encoding=encoding, source_dtype=node_info.dtype
    ) as extra:
        writer = CscWriter(
            node,
            encoding,
            (shape[0], shape[1]),
            opts.memory_bytes,
            work,
            prog,
            source_dtype=node_info.dtype,
        )
        if "X" in tg:
            del tg["X"]
        stats = writer.write(tg.create_group("X"))
        nnz = writer.nnz
        extra.update(nnz=nnz, **writer.facts)
    warnings += stats.warnings
    canopyg = anndata_dict_group(tg["uns"], "canopy")  # type: ignore[arg-type]
    _gene_stats(canopyg, stats)
    with timed("convert/downsample", version=version, table=name) as extra:
        sizes = _downsample(canopyg, obs, uns)
        extra.update(sizes=sizes)
    consolidate(tpath)
    region = region or {}
    return TableEntry(
        path=f"tables/{name}",
        n_obs=shape[0],
        n_vars=shape[1],
        nnz=nnz,
        x_source="raw/X" if from_raw else opts.x_source,
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
        var_names_column=gene_list(var).symbol_column,
        downsample=sizes,
        region=list(region.get("region", [])),
        region_key=region.get("region_key"),
        instance_key=region.get("instance_key"),
    )


def store_size(store: Path) -> int:
    """Bytes of a converted store: from `<store>.size`, written when it was published (walking
    every file of every store is slow on network storage; review full-2 A-L5), else measured
    once and written."""
    side = store.with_name(store.name + ".size")
    try:
        return int(side.read_text())
    except (OSError, ValueError):
        pass
    n = _walk_size(store)
    with contextlib.suppress(OSError):
        side.write_text(str(n))
    return n


def _walk_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.stat(os.path.join(root, f)).st_size
    return total


def _publish(tmp: Path, final: Path) -> None:
    """Move the new store in place; an existing one is renamed aside first and removed after,
    so readers never see a half-replaced store. Its size is kept beside it (`store_size`)."""
    size = _walk_size(tmp)
    old = None
    if final.exists():
        old = final.parent / f"{final.name}.old-{uuid.uuid4().hex[:8]}"
        final.rename(old)
    tmp.rename(final)
    with contextlib.suppress(OSError):
        final.with_name(final.name + ".size").write_text(str(size))
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def _matrix_info(info: AnnDataInfo, x_source: str) -> MatrixInfo:
    if x_source == "X" and info.X is None and info.raw is not None:
        return info.raw
    m = {"X": info.X, "raw/X": info.raw}.get(x_source)
    if x_source.startswith("layers/"):
        m = info.layers.get(x_source.split("/", 1)[1])
    if m is None:
        raise ConversionError("unknown_matrix", "the dataset has no such matrix")
    return m


# Spill files and the store per stored value (12 bytes of spill records; the store compresses
# to well under 8 bytes per value), with a margin.
SPILL_BYTES_PER_VALUE = 12
STORE_BYTES_PER_VALUE = 8
DISK_MARGIN = 1.1


def store_disk_estimate(fingerprint: str, x_source: str) -> int:
    """Bytes a conversion's store will take (for jobs.stores_max_gb)."""
    from canopy.inspect.models import Fingerprint

    try:
        fp = Fingerprint.model_validate_json(fingerprint)
    except ValueError:
        return 0
    if fp.anndata is not None:
        try:
            return disk_need(_matrix_info(fp.anndata, x_source))[0]
        except ConversionError:
            return 0
    if fp.spatialdata is not None:
        from canopy.convert.spatialdata import spatial_disk_estimate

        return spatial_disk_estimate(fp, x_source)[0]
    return 0


def disk_need(m: MatrixInfo) -> tuple[int, int]:
    """Bytes of (store, spill files) a table's conversion writes."""
    n_obs, n_vars = m.shape
    values = (m.nnz or 0) if m.encoding != "dense" else n_obs * n_vars
    spill = 0 if m.encoding == "csc" else int(values * SPILL_BYTES_PER_VALUE * DISK_MARGIN)
    return int(values * STORE_BYTES_PER_VALUE * DISK_MARGIN), spill


def check_disk(
    store_parent: Path, spill_root: Path, store: int, spill: int, version: str, log: Any
) -> None:
    """Refuse to start when the cache or the spill folder lacks the space the job needs."""
    store_parent.mkdir(parents=True, exist_ok=True)
    spill_root.mkdir(parents=True, exist_ok=True)
    same = os.stat(store_parent).st_dev == os.stat(spill_root).st_dev
    needs = {store_parent: store + (spill if same else 0)}
    if not same:
        needs[spill_root] = spill
    for path, need in needs.items():
        free = shutil.disk_usage(path).free
        log.info(
            "convert_disk",
            version=version,
            need_bytes=need,
            free_bytes=free,
            spill=path == spill_root,
        )
        if free < need:
            raise ConversionError(
                "not_enough_disk",
                f"needs {need / 2**30:.1f} GB free, {free / 2**30:.1f} GB available",
            )


def _check_disk(
    store_parent: Path, spill_root: Path, m: MatrixInfo, version: str, log: Any
) -> None:
    check_disk(store_parent, spill_root, *disk_need(m), version, log)


def write_manifest(tmp: Path, manifest: Manifest) -> None:
    """manifest.json; a non-finite number (which JSON cannot hold) fails the conversion."""
    try:
        text = json.dumps(manifest.model_dump(mode="json"), indent=2, allow_nan=False)
    except ValueError as e:
        raise ConversionError(
            "manifest_not_finite", "a NaN or infinite value in the manifest"
        ) from e
    (tmp / MANIFEST).write_text(text)


def log_invalid(log: Any, version: str, report: Any) -> None:
    """Log why a store failed validation (readable at strict: names tokenised, problems kept)
    and fail the conversion."""
    log.error(
        "store_invalid", version=version, n_errors=len(report.errors), problems=report.problems()
    )
    raise ConversionError("store_invalid", "the converted store failed validation")


def peak_rss_gb() -> float:
    """This process's peak resident memory so far, in GB."""
    import resource
    import sys

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round((peak if sys.platform == "darwin" else peak * 1024) / 2**30, 2)


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
        elif pd.api.types.is_bool_dtype(s.dtype):
            n = 2  # false, true: as the preview colours them (review full-2 B-L2)
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
        frame: np.ndarray | None = None
        if not hasattr(node, "shape"):  # a data frame or a sparse matrix
            kind = str(dict(getattr(node, "attrs", {})).get("encoding-type", ""))
            if kind == "dataframe":
                # its numeric columns, as the preview shows them (review full-2 B-L3)
                df: pd.DataFrame = read_elem(node)
                num = df.select_dtypes("number")
                if num.shape[1] >= 2:
                    frame = num.to_numpy(dtype=np.float32)
                    dims = frame.shape[1]
            if frame is None:
                warnings.append("obsm_sparse_skipped" if "matrix" in kind else "obsm_frame_skipped")
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
            a[s:e] = frame[s:e] if frame is not None else np.asarray(node[s:e], dtype=np.float32)
        out[key] = dims
        progress((k + 1) / max(len(keys), 1))
    skipped = set(info.obsm) - set(keys)
    if skipped:
        warnings.append("obsm_not_embedding_skipped")
    spatial = [k for k in out if k == "spatial" or k.startswith("spatial")]
    return {k: d for k, d in out.items() if k not in spatial}, spatial


def _gene_stats(canopyg: zarr.Group, stats: GeneStats) -> None:
    g = anndata_dict_group(canopyg, "gene_stats")
    for name in ("nnz", "mean", "max", "p99"):
        array(g, name, np.asarray(getattr(stats, name), dtype=np.float32), attrs=ANNDATA_ARRAY)


def downsample_indices(n_obs: int, n: int, groups: np.ndarray | None, seed: int = 0) -> np.ndarray:
    """`n` sorted cell indices; stratified by `groups` (codes, -1 = none) so every group keeps
    at least one cell, proportionally otherwise; uniform without groups. With more groups than
    `n`, one cell from each of the `n` largest groups."""
    rng = np.random.default_rng(seed)
    n = min(n, n_obs)
    if groups is None:
        return np.sort(rng.choice(n_obs, size=n, replace=False))
    codes = np.asarray(groups, dtype=np.int64) + 1  # missing → group 0
    counts = np.bincount(codes)
    present = np.flatnonzero(counts)
    quota = np.zeros(len(counts), dtype=np.int64)
    if len(present) >= n:
        quota[present[np.argsort(-counts[present], kind="stable")[:n]]] = 1
    else:
        want = counts * (n / n_obs)
        quota = np.minimum(np.maximum(np.floor(want).astype(np.int64), (counts > 0)), counts)
        short = n - int(quota.sum())
        # add by largest remainder (where room is left), or take back from the largest groups
        order = np.argsort(-(want - np.floor(want)), kind="stable")
        while short > 0:
            for g in order:
                if short == 0:
                    break
                if quota[g] < counts[g]:
                    quota[g] += 1
                    short -= 1
        while short < 0:
            for g in np.argsort(-quota, kind="stable"):
                if short == 0:
                    break
                if quota[g] > 1:
                    quota[g] -= 1
                    short += 1
    order = np.argsort(codes, kind="stable")
    bounds = np.concatenate([[0], np.cumsum(counts)])
    picked = [
        rng.choice(order[bounds[g] : bounds[g + 1]], size=int(quota[g]), replace=False)
        for g in range(len(counts))
        if quota[g]
    ]
    return np.sort(np.concatenate(picked)) if picked else np.zeros(0, np.int64)


# Names that usually hold the main annotation (cell type, cluster): the column the subsets are
# stratified by, as the browser's default colouring prefers them.
_ANNOTATION = re.compile(r"cell.?type|annotation|cluster|leiden|louvain", re.I)


def annotation_column(columns: list[tuple[str, int, bool]]) -> str | None:
    """The categorical column to stratify by, from (name, categories, has colours): cell type /
    cluster names first, then coloured columns, among those with 2–5,000 categories."""
    usable = [c for c in columns if 1 < c[1] <= 5_000]
    rank = lambda c: (0 if _ANNOTATION.search(c[0]) else 2) + (0 if c[2] else 1)  # noqa: E731
    return min(usable, key=rank)[0] if usable else None


def _downsample(canopyg: zarr.Group, obs: pd.DataFrame, uns: dict[str, Any]) -> list[int]:
    n_obs = len(obs)
    if n_obs <= DOWNSAMPLE_THRESHOLD:
        return []
    by = annotation_column(
        [
            (str(c), len(obs[c].cat.categories), f"{c}_colors" in uns)
            for c in obs.columns
            if isinstance(obs[c].dtype, pd.CategoricalDtype)
        ]
    )
    groups = obs[by].cat.codes.to_numpy() if by else None
    g = anndata_dict_group(canopyg, "downsample")
    dtype = np.int32 if n_obs < 2**31 else np.int64
    sizes = [n for n in DOWNSAMPLE_SIZES if n < n_obs]
    for n in sizes:
        idx = downsample_indices(n_obs, n, groups).astype(dtype)
        array(g, str(n), idx, attrs=ANNDATA_ARRAY)
    return sizes
