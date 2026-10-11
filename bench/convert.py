"""Conversion benchmark: time and peak memory of converting each h5ad / AnnData-zarr file in a
folder, at one or more working-memory budgets.

    pixi run -e dev python bench/convert.py <folder> [--budget-gb 1 0.5]

Prints one Markdown table row per file and budget (see bench/README.md for recorded results).
Stores are written to a temporary cache folder that is removed afterwards.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import threading
import time
from pathlib import Path

import psutil

from dbx.convert.anndata import ConvertOptions, convert_anndata
from dbx.inspect import inspect_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--budget-gb", type=float, nargs="+", default=[1.0])
    args = ap.parse_args()
    print("| file | layout | values | budget GB | seconds | peak RSS GB |")
    print("|---|---|---|---|---|---|")
    for f in sorted(args.folder.iterdir()):
        if f.suffix not in (".h5ad", ".zarr"):
            continue
        fp = inspect_path(f)
        if fp.anndata is None or fp.anndata.X is None:
            continue
        for gb in args.budget_gb:
            cache = Path(tempfile.mkdtemp(prefix="dbx-bench-convert-"))
            proc = psutil.Process()
            peak = [proc.memory_info().rss]
            stop = threading.Event()

            threading.Thread(target=_watch, args=(proc, peak, stop), daemon=True).start()
            t0 = time.monotonic()
            r = convert_anndata(
                f, fp, cache, ConvertOptions(memory_bytes=int(gb * 2**30)), lambda s, x: None
            )
            stop.set()
            t = r.manifest.tables["table"]
            print(
                f"| {f.name} | {fp.anndata.X.encoding} | {t.nnz:,} | {gb} | "
                f"{time.monotonic() - t0:.1f} | {peak[0] / 2**30:.2f} |"
            )
            shutil.rmtree(cache, ignore_errors=True)


def _watch(proc: psutil.Process, peak: list[int], stop: threading.Event) -> None:
    while not stop.wait(0.1):
        peak[0] = max(peak[0], proc.memory_info().rss)


if __name__ == "__main__":
    main()
