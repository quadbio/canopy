"""Compare transcript layouts of contract v0.1 and the v0.2 draft on a real points element.

Measures what the browser would download for typical views:
  - one gene, whole tissue        (v0.1: whole 1 M-row chunks; v0.2: 65,536-row chunks)
  - one large gene, zoomed in     (v0.1: whole gene range; v0.2: only chunks whose bbox is in view)
  - zoomed-out "all transcripts"  (v0.1: index list -> all xy; v0.2: materialised overview)

Usage: pixi run -e dev python bench/contract_v02_points.py POINTS_PARQUET OUT_DIR [--feature COL]
Writes the arrays to OUT_DIR (delete afterwards) and prints a Markdown table.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
import pyarrow.dataset as ds
import zarr

from canopy.contract.write import array, morton_order

V01_CHUNK = 1_048_576
V02_CHUNK = 65_536
OVERVIEW = 1_000_000
LOD_CAP = 100_000


def _chunk_bytes(arr_dir: Path) -> dict[int, int]:
    """Compressed size of each chunk file of a 2-D (N, k) or 1-D array, by chunk row index."""
    out: dict[int, int] = {}
    for f in (arr_dir / "c").rglob("*"):
        if f.is_file():
            out[int(f.relative_to(arr_dir / "c").parts[0])] = f.stat().st_size
    return out


def _fetch(chunks: dict[int, int], chunk: int, lo: int, hi: int) -> int:
    if hi <= lo:
        return 0
    return sum(chunks.get(i, 0) for i in range(lo // chunk, (hi - 1) // chunk + 1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("parquet", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--feature", default="gene_id")
    ap.add_argument("--chunk", type=int, default=V02_CHUNK, help="v0.2 rows per chunk")
    a = ap.parse_args()
    chunk = a.chunk

    t = ds.dataset(a.parquet).to_table(columns=["x", "y", a.feature])
    x = t.column("x").to_numpy().astype(np.float32)
    y = t.column("y").to_numpy().astype(np.float32)
    names, feat = np.unique(t.column(a.feature).to_numpy(zero_copy_only=False), return_inverse=True)
    n, nf = len(x), len(names)
    bbox = (float(x.min()), float(y.min()), float(x.max()), float(y.max()))
    order = np.lexsort((morton_order(x, y, bbox), feat))
    xy = np.stack([x, y], 1)[order]
    feat = feat[order].astype(np.int32)
    counts = np.bincount(feat, minlength=nf)
    offsets = np.concatenate([[0], np.cumsum(counts)])

    if a.out.exists():
        shutil.rmtree(a.out)
    root = zarr.open_group(a.out, mode="w")
    array(root, "v01_xy", xy, chunks=(V01_CHUNK, 2))
    array(root, "v02_xy", xy, chunks=(chunk, 2))
    rng = np.random.default_rng(0)
    ov_idx = np.sort(rng.choice(n, min(n, OVERVIEW), replace=False))
    array(root, "v01_overview", ov_idx.astype(np.int32), chunks=(V01_CHUNK,))
    array(root, "v02_overview_xy", xy[ov_idx], chunks=(V02_CHUNK, 2))
    array(root, "v02_overview_feature", feat[ov_idx], chunks=(V02_CHUNK,))
    c01 = _chunk_bytes(a.out / "v01_xy")
    c02 = _chunk_bytes(a.out / "v02_xy")

    # one gene, whole tissue
    g01 = np.array([_fetch(c01, V01_CHUNK, offsets[g], offsets[g + 1]) for g in range(nf)])
    g02 = np.array([_fetch(c02, chunk, offsets[g], offsets[g + 1]) for g in range(nf)])
    raw = counts * 8  # bytes of float32 x, y actually belonging to the gene

    # largest gene, zoomed to the central 10 % x 10 % of the tissue
    big = int(np.argmax(counts))
    lo, hi = offsets[big], offsets[big + 1]
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    w, h = (bbox[2] - bbox[0]) * 0.05, (bbox[3] - bbox[1]) * 0.05
    view = (cx - w, cy - h, cx + w, cy + h)
    zoom01 = _fetch(c01, V01_CHUNK, lo, hi)
    zoom02, in_view = 0, 0
    for c in range(lo // chunk, (hi - 1) // chunk + 1):
        s, e = max(lo, c * chunk), min(hi, (c + 1) * chunk)
        p = xy[s:e]
        bx0, by0 = p.min(0)
        bx1, by1 = p.max(0)
        if bx1 >= view[0] and bx0 <= view[2] and by1 >= view[1] and by0 <= view[3]:
            zoom02 += c02.get(c, 0)
        inside = (p[:, 0] >= view[0]) & (p[:, 0] <= view[2])
        inside &= (p[:, 1] >= view[1]) & (p[:, 1] <= view[3])
        in_view += int(inside.sum())

    def size(name: str) -> int:
        return sum(f.stat().st_size for f in (a.out / name).rglob("*") if f.is_file())

    ov01 = size("v01_overview") + size("v01_xy")
    ov02 = size("v02_overview_xy") + size("v02_overview_feature")
    over_cap = counts[counts > LOD_CAP]

    mb = 2**20
    print(f"v0.2 chunk: {chunk:,} rows")
    print(
        f"points: {n:,}; genes: {nf}; median per gene {int(np.median(counts)):,}; "
        f"largest {counts.max():,}; genes above {LOD_CAP:,}: {len(over_cap)}"
    )
    print(
        f"xy total compressed: {size('v01_xy') / mb:.1f} MB (v0.1), "
        f"{size('v02_xy') / mb:.1f} MB (v0.2); chunk files {len(c01)} vs {len(c02)} "
        f"(v0.2 groups them into {len(c01)} shard files)"
    )
    print()
    print("| View | Useful data | v0.1 download | v0.2 download |")
    print("|---|---|---|---|")
    for label, q in (("median gene", 50), ("90th percentile gene", 90)):
        gi = int(np.argsort(counts)[int(len(counts) * q / 100)])
        print(
            f"| One gene, whole tissue ({label}, {counts[gi]:,} points) | "
            f"{raw[gi] / 1024:.0f} KB | {g01[gi] / mb:.2f} MB | {g02[gi] / mb:.2f} MB |"
        )
    print(
        f"| One gene, whole tissue (average over all genes) | {raw.mean() / 1024:.0f} KB | "
        f"{g01.mean() / mb:.2f} MB | {g02.mean() / mb:.2f} MB |"
    )
    print(
        f"| Largest gene ({counts[big]:,} points), zoomed to 10 % x 10 % "
        f"({in_view:,} points in view) | {in_view * 8 / 1024:.0f} KB | "
        f"{zoom01 / mb:.2f} MB | {zoom02 / mb:.2f} MB |"
    )
    print(
        f"| All transcripts, zoomed out ({len(ov_idx):,}-point overview) | "
        f"{len(ov_idx) * 12 / mb:.1f} MB | {ov01 / mb:.1f} MB | {ov02 / mb:.1f} MB |"
    )


if __name__ == "__main__":
    main()
