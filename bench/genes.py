"""Gene request benchmark: time per gene for each file in a folder, through the real API.

    pixi run -e dev python bench/genes.py <folder of .h5ad/.zarr> [--genes 20] [--parallel 8]

For each dataset: the first gene (for a cell-major matrix this builds the in-memory copy), then
`--genes` further genes, then `--parallel` concurrent requests for new genes. Prints the timings,
reply sizes and the server's peak memory per stage (from its log), as a Markdown table row.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from dbx.app import create_app
from dbx.catalog import entry_id
from dbx.config import Config, DataRoot, LoggingConfig, ServerConfig
from dbx.logs import current_log_file, setup_logging


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--genes", type=int, default=20)
    ap.add_argument("--parallel", type=int, default=8)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="dbx-bench-"))
    cfg = Config(
        data_roots=[DataRoot(name="bench", path=args.folder)],
        cache_dir=tmp / "cache",
        server=ServerConfig(allowed_hosts=["testserver"]),
        logging=LoggingConfig(dir=tmp / "logs", memory_sample_interval=0),
    )
    setup_logging(cfg.logging, role="bench", salt="bench", to_console=False)
    app = create_app(cfg, background=False)
    app.state.catalog.scan_now()
    c = TestClient(app)
    print(
        "| file | layout / access | first gene | next genes (median, max) | "
        f"{args.parallel} parallel new genes (wall) | KB per gene | peak RSS MB |"
    )
    print("|---|---|---|---|---|---|---|")
    for f in sorted(args.folder.iterdir()):
        if f.suffix not in (".h5ad", ".zarr"):
            continue
        id = entry_id("bench", f.name)
        meta = c.get(f"/api/datasets/{id}/meta").json()
        x = next(m for m in meta["matrices"] if m["name"] == "X")
        n_vars = meta["n_vars"]
        step = max(1, n_vars // (args.genes + args.parallel + 2))

        def gene(i: int, id: str = id) -> tuple[float, int, int]:
            t = time.perf_counter()
            r = c.get(f"/api/datasets/{id}/gene", params={"i": i, "q": 16})
            return time.perf_counter() - t, r.status_code, len(r.content)

        first, status, size = gene(0)
        if status != 200:
            print(f"| {f.name} | {x['encoding']} / {x['access']} | HTTP {status} | | | | |")
            continue
        times, sizes = [], []
        for k in range(1, args.genes + 1):
            t, _, n = gene(k * step)
            times.append(t)
            sizes.append(n)
        start = (args.genes + 1) * step
        t0 = time.perf_counter()
        with ThreadPoolExecutor(args.parallel) as pool:
            list(pool.map(gene, [start + k * step for k in range(args.parallel)]))
        wall = time.perf_counter() - t0
        peak = max(
            (e.get("rss_peak_mb", 0) for e in _events() if e.get("dataset_id") == id), default=0
        )
        print(
            f"| {f.name} | {x['encoding']} / {x['access']} | {first:.2f} s | "
            f"{statistics.median(times) * 1000:.0f} ms, {max(times) * 1000:.0f} ms | "
            f"{wall:.2f} s | {statistics.mean(sizes) / 1024:.0f} | {peak:.0f} |"
        )


def _events() -> list[dict[str, object]]:
    f = current_log_file()
    return [json.loads(line) for line in f.read_text().splitlines()] if f else []


if __name__ == "__main__":
    main()
