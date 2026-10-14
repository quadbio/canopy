"""Write a source matrix (CSR, CSC or dense; h5py or zarr) as the contract's gene-major CSC X,
out of core, with per-gene statistics (SPEC §4.1, §4.4).

Memory stays within `budget_bytes` whatever the matrix size:
1. **Count** the stored entries per gene (one pass over the indices; free for CSC sources).
2. **Spill** (CSR and dense sources): one pass over the cells in row blocks; each entry goes to
   the temporary file of its *gene bucket* (a run of consecutive genes whose entries fit the
   budget). Disk use: 12 bytes per stored value (16 above 2^31 cells), in `tmp_dir`; entries
   are buffered per bucket and appended in blocks of a few MB.
3. **Write** bucket by bucket: load one bucket, group its entries by gene (rows stay ascending,
   so the result is canonical CSC), write it as one contiguous range of `data`/`indices`, and
   compute the bucket's gene statistics.
A CSC source skips the spill: each bucket is read straight from the source; rows are sorted
within a gene only where the source left them unsorted.

`facts` (budget, buckets, blocks, spill bytes, free space) go into the converter's log line.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import zarr

from canopy.contract.models import INDPTR_CHUNK, X_CHUNK
from canopy.contract.write import array, codecs

Progress = Callable[[str, float], None]  # (stage, fraction 0..1 of this stage)
BYTES_PER_ENTRY = 72


class ConversionError(Exception):
    """The source cannot be converted; `code` is a short reason for logs and the UI."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class GeneStats:
    nnz: np.ndarray  # non-zero values per gene
    mean: np.ndarray
    max: np.ndarray
    p99: np.ndarray  # 99th percentile of the non-zero values
    warnings: list[str] = field(default_factory=list)


def _row_blocks(n_rows: int, per_row: float, budget_values: int) -> list[tuple[int, int]]:
    rows = max(1, int(budget_values / max(per_row, 1.0)))
    return [(a, min(a + rows, n_rows)) for a in range(0, n_rows, rows)]


def _buckets(counts: np.ndarray, budget_values: int) -> list[tuple[int, int]]:
    """Consecutive gene ranges whose stored entries fit `budget_values` (a gene larger than that
    gets a bucket of its own)."""
    out: list[tuple[int, int]] = []
    start, acc = 0, 0
    for j, c in enumerate(counts.tolist()):
        if acc and acc + c > budget_values:
            out.append((start, j))
            start, acc = j, 0
        acc += c
    out.append((start, len(counts)))
    return [b for b in out if b[1] > b[0]]


class CscWriter:
    def __init__(
        self,
        node: Any,
        encoding: str,
        shape: tuple[int, int],
        budget_bytes: int,
        tmp_dir: Path,
        progress: Progress,
        source_dtype: str = "float32",
    ) -> None:
        self.node = node
        self.encoding = encoding
        self.n_obs, self.n_vars = shape
        # Working memory per entry in flight, measured: the entry itself plus the sort keys, the
        # order and the reordered copies (300 k x 20 k, 120 M values: peak within the budget).
        self.budget_values = max(100_000, budget_bytes // BYTES_PER_ENTRY)
        self.tmp_dir = tmp_dir
        self.progress = progress
        self.idx_dtype = np.int32 if self.n_obs < 2**31 else np.int64
        self.source_dtype = np.dtype(source_dtype)
        self.facts: dict[str, Any] = {"budget_values": self.budget_values}

    # -------------------------------------------------------------------- public
    def write(self, x: zarr.Group) -> GeneStats:
        t0 = time.monotonic()
        counts = self._counts()
        self.facts["count_s"] = round(time.monotonic() - t0, 1)
        indptr = np.zeros(self.n_vars + 1, dtype=np.int64)
        np.cumsum(counts, out=indptr[1:])
        nnz = int(indptr[-1])
        self.nnz = nnz
        x.attrs.update(
            {
                "encoding-type": "csc_matrix",
                "encoding-version": "0.1.0",
                "shape": [self.n_obs, self.n_vars],
            }
        )
        data = x.create_array(
            "data", shape=(nnz,), dtype=np.float32, chunks=(X_CHUNK,), compressors=codecs()
        )
        indices = x.create_array(
            "indices", shape=(nnz,), dtype=self.idx_dtype, chunks=(X_CHUNK,), compressors=codecs()
        )
        array(x, "indptr", indptr, chunks=(INDPTR_CHUNK,))
        buckets = _buckets(counts, self.budget_values)
        sizes = [int(indptr[b] - indptr[a]) for a, b in buckets]
        self.facts.update(
            n_buckets=len(buckets),
            largest_bucket=max(sizes, default=0),
            oversize_buckets=sum(1 for v in sizes if v > self.budget_values),
        )
        z = self.n_vars
        stats = GeneStats(
            np.zeros(z, np.float32),
            np.zeros(z, np.float32),
            np.zeros(z, np.float32),
            np.zeros(z, np.float32),
        )
        if self.encoding == "csc":
            source = self._csc_bucket
        else:
            t1 = time.monotonic()
            spill = self._spill(buckets)
            source = spill.read
            self.facts["spill_s"] = round(time.monotonic() - t1, 1)
            self.facts.update(
                spill_bytes=spill.bytes,
                tmp_free_bytes=shutil.disk_usage(self.tmp_dir).free,
            )
        nonfinite = 0
        t2 = time.monotonic()
        for k, (a, b) in enumerate(buckets):
            rows, cols, vals = source(a, b)
            lo, hi = int(indptr[a]), int(indptr[b])
            if len(rows) != hi - lo:
                raise ConversionError("inconsistent_matrix", "entry counts changed between passes")
            if self.encoding != "csc":
                # Group by gene, rows staying ascending within one: a stable sort on the gene's
                # position in the bucket (16 bits when it fits: numpy then uses a linear-time
                # radix sort). CSC sources come grouped already.
                local = (cols - a).astype(np.uint16 if b - a <= 2**16 else np.int32)
                del cols
                order = np.argsort(local, kind="stable")
                rows, vals = rows[order], vals[order]
                cols = local[order].astype(np.int64) + a
                del order, local
            self._check_canonical(rows, cols, a)
            if hi > lo:
                indices[lo:hi] = rows.astype(self.idx_dtype, copy=False)
                data[lo:hi] = vals
            self._stats(stats, a, b, cols, vals, indptr)
            nonfinite += int(len(vals) - np.isfinite(vals).sum())
            self.progress("write", (k + 1) / len(buckets))
        if self.encoding != "csc":
            spill.close()
        if nonfinite:
            stats.warnings.append(f"non_finite_values:{nonfinite}")
        # SPEC §4.2: integer counts above 2^24 are not exact in float32
        if self.source_dtype.kind in "iu" and nnz and float(stats.max.max()) > 2**24:
            stats.warnings.append("integers_above_2_24")
        self.facts["non_finite"] = nonfinite
        self.facts["write_s"] = round(time.monotonic() - t2, 1)
        return stats

    # -------------------------------------------------------------------- pass 1: counts
    def _counts(self) -> np.ndarray:
        if self.encoding == "csc":
            ip = np.asarray(self.node["indptr"][:], dtype=np.int64)
            return np.diff(ip)
        counts = np.zeros(self.n_vars, dtype=np.int64)
        if self.encoding == "csr":
            ip = np.asarray(self.node["indptr"][:], dtype=np.int64)
            self._indptr = ip
            per_row = ip[-1] / max(self.n_obs, 1)
            blocks = _row_blocks(self.n_obs, per_row, self.budget_values)
            for k, (a, b) in enumerate(blocks):
                idx = np.asarray(self.node["indices"][int(ip[a]) : int(ip[b])], dtype=np.int64)
                if idx.size and (idx.min() < 0 or idx.max() >= self.n_vars):
                    raise ConversionError("index_out_of_range", "a gene index is out of range")
                counts += np.bincount(idx, minlength=self.n_vars)
                self.progress("count", (k + 1) / len(blocks))
            return counts
        blocks = _row_blocks(self.n_obs, self.n_vars, self.budget_values)
        for k, (a, b) in enumerate(blocks):
            blk = np.asarray(self.node[a:b, :])
            counts += np.count_nonzero(blk, axis=0)  # NaN counts as stored
            self.progress("count", (k + 1) / len(blocks))
        return counts

    # -------------------------------------------------------------------- CSC source
    def _csc_bucket(self, a: int, b: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        ip = np.asarray(self.node["indptr"][a : b + 1], dtype=np.int64)
        lo, hi = int(ip[0]), int(ip[-1])
        rows = np.asarray(self.node["indices"][lo:hi], dtype=self.idx_dtype)
        vals = np.asarray(self.node["data"][lo:hi], dtype=np.float32)
        cols = np.repeat(np.arange(a, b, dtype=np.int64), np.diff(ip))
        # Rows may be unsorted within a gene in the source: sort only then, by (gene, row).
        if len(rows) > 1:
            step = np.diff(rows.astype(np.int64))
            new_gene = np.diff(cols) != 0
            if np.any((step < 0) & ~new_gene):
                order = np.lexsort((rows, cols))
                rows, cols, vals = rows[order], cols[order], vals[order]
        return rows, cols, vals

    # -------------------------------------------------------------------- spill (CSR / dense)
    def _spill(self, buckets: list[tuple[int, int]]) -> _Spill:
        spill = _Spill(self.tmp_dir, buckets, self.n_obs)
        starts = np.array([a for a, _ in buckets], dtype=np.int64)
        if self.encoding == "csr":
            ip = self._indptr
            per_row = ip[-1] / max(self.n_obs, 1)
            blocks = _row_blocks(self.n_obs, per_row, self.budget_values)
        else:
            blocks = _row_blocks(self.n_obs, self.n_vars, self.budget_values)
        for k, (a, b) in enumerate(blocks):
            if self.encoding == "csr":
                lo, hi = int(ip[a]), int(ip[b])
                cols = np.asarray(self.node["indices"][lo:hi], dtype=np.int64)
                vals = np.asarray(self.node["data"][lo:hi], dtype=np.float32)
                rows = np.repeat(np.arange(a, b, dtype=np.int64), np.diff(ip[a : b + 1]))
            else:
                blk = np.asarray(self.node[a:b, :])
                r, cols = np.nonzero(blk)
                vals = blk[r, cols].astype(np.float32)
                rows = r.astype(np.int64) + a
                cols = cols.astype(np.int64)
            which = (np.searchsorted(starts, cols, side="right") - 1).astype(
                np.uint16 if len(buckets) <= 2**16 else np.int32
            )
            order = np.argsort(which, kind="stable")  # keeps the cell order within a bucket
            which, rows, cols, vals = which[order], rows[order], cols[order], vals[order]
            del order
            bounds = np.searchsorted(which, np.arange(len(buckets) + 1))
            for q in range(len(buckets)):
                s, e = int(bounds[q]), int(bounds[q + 1])
                if e > s:
                    spill.append(q, rows[s:e], cols[s:e], vals[s:e])
            self.progress("reorder", (k + 1) / len(blocks))
        spill.flush()
        return spill

    # -------------------------------------------------------------------- checks and stats
    def _check_canonical(self, rows: np.ndarray, cols: np.ndarray, a: int) -> None:
        if len(rows) < 2:
            return
        same = cols[1:] == cols[:-1]
        if np.any(rows[1:][same] == rows[:-1][same]):
            # a malformed matrix: the preview adds them (as scipy does), the converter refuses
            # and says how to repair the file (review full-2 B-L5)
            raise ConversionError(
                "duplicate_entries",
                "the matrix stores some cells twice for the same gene; repair it with "
                "adata.X.sum_duplicates() and save the file again",
            )

    def _stats(
        self,
        st: GeneStats,
        a: int,
        b: int,
        cols: np.ndarray,
        vals: np.ndarray,
        indptr: np.ndarray,
    ) -> None:
        if not len(vals):
            return
        local = cols - a
        finite = np.isfinite(vals)
        nonzero = finite & (vals != 0)
        n = b - a
        st.nnz[a:b] = np.bincount(local[nonzero], minlength=n)
        sums = np.bincount(local[finite], weights=vals[finite].astype(np.float64), minlength=n)
        st.mean[a:b] = sums / max(self.n_obs, 1)
        mx = np.full(n, -np.inf)
        np.maximum.at(mx, local[finite], vals[finite])
        stored = np.diff(indptr[a : b + 1])
        mx = np.where(stored < self.n_obs, np.maximum(mx, 0.0), mx)  # unstored cells are 0
        st.max[a:b] = np.where(np.isfinite(mx), mx, 0.0)
        # p99 of the non-zero values, gene by gene (values are grouped by gene already)
        nzv, nzc = vals[nonzero], local[nonzero]
        edges = np.searchsorted(nzc, np.arange(n + 1))
        for j in range(n):
            s, e = int(edges[j]), int(edges[j + 1])
            if e > s:
                st.p99[a + j] = np.percentile(nzv[s:e], 99)


class _Spill:
    """Per-bucket temporary files of (row, gene, value) entries, appended in cell order."""

    def __init__(self, tmp_dir: Path, buckets: list[tuple[int, int]], n_obs: int) -> None:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        self.dir = tmp_dir
        self.paths = [tmp_dir / f"bucket-{q:05d}" for q in range(len(buckets))]
        self.index = {a: q for q, (a, _) in enumerate(buckets)}
        # 12 bytes per entry on disk while fewer than 2^31 cells (16 above)
        self.dtype = np.dtype(
            [("r", "<i4" if n_obs < 2**31 else "<i8"), ("c", "<i4"), ("v", "<f4")]
        )
        self.pending: dict[int, list[np.ndarray]] = {}
        self.pending_bytes: dict[int, int] = {}
        self.held = 0
        self.counts = [0] * len(buckets)
        self.bytes = 0

    BUFFER = 64 * 2**20  # entries held in memory before the fullest buckets are written out

    def append(self, q: int, rows: np.ndarray, cols: np.ndarray, vals: np.ndarray) -> None:
        rec = np.empty(len(rows), dtype=self.dtype)
        rec["r"], rec["c"], rec["v"] = rows, cols, vals
        self.pending.setdefault(q, []).append(rec)
        self.pending_bytes[q] = self.pending_bytes.get(q, 0) + rec.nbytes
        self.held += rec.nbytes
        self.counts[q] += len(rows)
        self.bytes += rec.nbytes
        if self.held > self.BUFFER:
            for big in sorted(self.pending_bytes, key=lambda k: -self.pending_bytes[k]):
                self._write(big)
                if self.held <= self.BUFFER // 2:
                    break

    def _write(self, q: int) -> None:
        recs = self.pending.pop(q, [])
        if not recs:
            return
        with open(self.paths[q], "ab") as f:
            for r in recs:
                f.write(r.tobytes())
        self.held -= self.pending_bytes.pop(q, 0)

    def flush(self) -> None:
        for q in list(self.pending):
            self._write(q)

    def read(self, a: int, b: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        path = self.paths[self.index[a]]
        if not path.exists():
            empty = np.zeros(0, np.int64)
            return empty, empty, np.zeros(0, np.float32)
        rec = np.fromfile(path, dtype=self.dtype)
        os.remove(path)  # free the disk space as soon as the bucket is written
        rows, cols, vals = rec["r"].copy(), rec["c"].copy(), rec["v"].copy()
        del rec
        return rows, cols, vals

    def close(self) -> None:
        self.flush()
        for p in self.paths:
            p.unlink(missing_ok=True)
