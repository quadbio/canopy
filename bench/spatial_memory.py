"""SpatialData conversion: measured peak memory against the memory guard's estimate.

    pixi run -e dev python bench/spatial_memory.py [--scale 1.0] [--threads 4] [--keep DIR]

Writes one synthetic SpatialData per element kind (transcripts with typical extra columns, a
sparse-ID mask, chunked and single-chunk images, polygons, a table), converts each in a fresh
process and prints one Markdown row per case: time, peak memory above the interpreter baseline,
the estimate (`spatial_memory_estimate`), their ratio and the job watchdog's limit. The estimate
must stay at or above the peak (ratio ≥ 1) for what is read whole (points, shapes, image and mask
blocks); a table's matrix is covered by the separate matrix budget (jobs.memory_gb). See
bench/README.md for recorded results. Generated data is removed afterwards.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

CASES = [
    "points_int_cell",
    "points_str_cell",
    "mask_sparse",
    "image_chunked",
    "image_single",
    "polygons",
    "table",
]


def write_case(case: str, path: Path, scale: float) -> None:
    import anndata as ad
    import dask.dataframe as dd
    import geopandas as gpd
    import pandas as pd
    import scipy.sparse as sp
    from shapely import box
    from spatialdata import SpatialData
    from spatialdata.models import Image2DModel, Labels2DModel, PointsModel, ShapesModel, TableModel

    rng = np.random.default_rng(0)
    if case.startswith("points"):
        n = int(10_000_000 * scale)
        genes = np.array([f"g{i}" for i in range(300)])
        df = pd.DataFrame(
            {
                "x": rng.uniform(0, 20_000, n),
                "y": rng.uniform(0, 20_000, n),
                "z": rng.integers(0, 7, n).astype(np.float64),
                "gene": pd.Categorical.from_codes(rng.integers(0, 300, n), genes),
                "qv": rng.uniform(0, 40, n).astype(np.float32),
                "fov": rng.integers(0, 400, n),
            }
        )
        cells = rng.integers(0, 200_000, n)
        df["cell_id"] = cells if case == "points_int_cell" else pd.Series(cells).map("c{}".format)
        if case == "points_str_cell":
            df["transcript_id"] = pd.Series(np.arange(n)).map("t{}".format)
        ddf = dd.from_pandas(df, npartitions=max(1, n // 1_000_000))
        pts = PointsModel.parse(ddf, feature_key="gene", instance_key="cell_id")
        SpatialData(points={"tx": pts}).write(path)
    elif case == "mask_sparse":
        side = int(12_000 * np.sqrt(scale))
        mask = np.zeros((side, side), np.int64)
        k = 0
        for y in range(0, side - 20, 24):
            for x in range(0, side - 20, 24):
                k += 1
                mask[y : y + 20, x : x + 20] = 1_000_000_000 + k * 977
        lab = Labels2DModel.parse(mask, dims=("y", "x"), chunks=(4096, 4096))
        SpatialData(labels={"cells": lab}).write(path)
    elif case.startswith("image"):
        side = int(12_000 * np.sqrt(scale))
        img = rng.integers(0, 4000, (2, side, side), dtype=np.uint16)
        chunks = (1, 4096, 4096) if case == "image_chunked" else (2, side, side)
        SpatialData(
            images={"img": Image2DModel.parse(img, dims=("c", "y", "x"), chunks=chunks)}
        ).write(path)
    elif case == "polygons":
        n = int(300_000 * scale)
        x, y = rng.uniform(0, 50_000, n), rng.uniform(0, 50_000, n)
        geoms = box(x, y, x + 8, y + 8)
        shapes = ShapesModel.parse(gpd.GeoDataFrame(geometry=geoms))
        SpatialData(shapes={"cells": shapes}).write(path)
    elif case == "table":
        n_obs, n_vars = int(400_000 * scale), 3000
        per = int(n_vars * 0.05)  # 5 % of the genes in every cell
        cols = np.sort(rng.integers(0, n_vars, (n_obs, per)), axis=1)  # repeats summed below
        indptr = np.arange(0, n_obs * per + 1, per, dtype=np.int64)
        data = rng.random(n_obs * per, dtype=np.float32)
        x = sp.csr_matrix((data, cols.ravel(), indptr), shape=(n_obs, n_vars))
        x.sum_duplicates()
        obs = pd.DataFrame(
            {"region": pd.Categorical(["cells"] * n_obs), "cell_id": np.arange(n_obs)},
            index=[f"c{i}" for i in range(n_obs)],
        )
        t = TableModel.parse(
            ad.AnnData(X=x, obs=obs), region="cells", region_key="region", instance_key="cell_id"
        )
        SpatialData(tables={"table": t}).write(path)


def measure(src: Path, threads: int) -> dict[str, Any]:
    """Convert in this process (run as a child): seconds, peak RSS above the baseline, estimate."""
    import resource
    import time

    import psutil

    from dbx.convert.anndata import ConvertOptions
    from dbx.convert.spatialdata import convert_spatialdata, spatial_memory_estimate
    from dbx.inspect import inspect_path

    fp = inspect_path(src)
    base = psutil.Process().memory_info().rss
    cache = Path(tempfile.mkdtemp(prefix="dbx-bench-spatial-"))
    t0 = time.monotonic()
    convert_spatialdata(src, fp, cache, ConvertOptions(threads=threads), lambda s, f: None)
    seconds = time.monotonic() - t0
    shutil.rmtree(cache, ignore_errors=True)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = peak if sys.platform == "darwin" else peak * 1024
    est = spatial_memory_estimate(fp, threads)
    budget = ConvertOptions().memory_bytes  # the tables' matrix budget (jobs.memory_gb)
    return {
        "seconds": round(seconds, 1),
        "peak_gb": round((peak - base) / 2**30, 2),
        "estimate_gb": round(est / 2**30, 2),
        # what the job's watchdog allows (jobs/worker.py): 2 × budget + 1 GB + 1.5 × estimate
        "limit_gb": round((2 * budget + 2**30 + 1.5 * est) / 2**30, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--cases", nargs="+", default=CASES)
    ap.add_argument("--keep", type=Path, default=None, help="folder for the generated data (kept)")
    ap.add_argument("--child", type=Path, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--write", nargs=2, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.child:
        print(json.dumps(measure(args.child, args.threads)))
        return
    if args.write:
        write_case(args.write[0], Path(args.write[1]), args.scale)
        return
    root = args.keep or Path(tempfile.mkdtemp(prefix="dbx-bench-spatial-src-"))
    head = (
        "| case | seconds | peak GB (above baseline) | estimate GB | estimate / peak | limit GB |"
    )
    print(head)
    print("|---|---|---|---|---|---|")
    try:
        for case in args.cases:
            src = root / f"{case}.zarr"
            if not src.exists():
                # each case is written and converted in a fresh process (clean peak memory)
                subprocess.run(  # noqa: S603 (this script with our interpreter)
                    [
                        sys.executable,
                        __file__,
                        "--write",
                        case,
                        str(src),
                        "--scale",
                        str(args.scale),
                    ],
                    check=True,
                )
            out = (
                subprocess.run(  # noqa: S603 (this script with our interpreter)
                    [sys.executable, __file__, "--child", str(src), "--threads", str(args.threads)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
                .splitlines()[-1]
            )
            r = json.loads(out)
            ratio = r["estimate_gb"] / max(r["peak_gb"], 0.01)
            print(
                f"| {case} | {r['seconds']} | {r['peak_gb']} | {r['estimate_gb']} | {ratio:.2f} "
                f"| {r['limit_gb']} |",
                flush=True,
            )
    finally:
        if args.keep is None:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
