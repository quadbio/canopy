"""A deterministic sparse matrix defined by hashing (row, column).

The same matrix can be produced in row blocks (for CSR/dense writing) or column blocks (for CSC
writing) without ever holding it whole, so CSR and CSC test files contain identical values and
can be compared in golden tests. Entry (i, j) is non-zero with probability `density`; its value
is an integer count in 1..max_count stored with the requested dtype.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

_M1 = np.uint64(0xBF58476D1CE4E5B9)
_M2 = np.uint64(0x94D049BB133111EB)
# Cells evaluated per hashing step; bounds temporary memory (~8 bytes each, a few arrays).
_BLOCK_CELLS = 8_000_000


def _mix(x: np.ndarray) -> np.ndarray:
    """splitmix64 finaliser (vectorised, wraps on overflow by design)."""
    with np.errstate(over="ignore"):
        x = (x ^ (x >> np.uint64(30))) * _M1
        x = (x ^ (x >> np.uint64(27))) * _M2
        out: np.ndarray = x ^ (x >> np.uint64(31))
        return out


@dataclass(frozen=True)
class SynthMatrix:
    n_obs: int
    n_vars: int
    density: float
    seed: int = 0
    dtype: str = "float32"
    max_count: int = 20

    def _hash(self, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
        r = rows.astype(np.uint64)[:, None]
        c = cols.astype(np.uint64)[None, :]
        with np.errstate(over="ignore"):
            key = (r * np.uint64(self.n_vars) + c) ^ (np.uint64(self.seed) * _M2)
        return _mix(key)

    def _dense_rows(self, i0: int, i1: int, j0: int = 0, j1: int | None = None) -> np.ndarray:
        j1 = self.n_vars if j1 is None else j1
        h = self._hash(np.arange(i0, i1), np.arange(j0, j1))
        threshold = np.uint64(min(int(self.density * 2**64), 2**64 - 1))
        mask = h < threshold
        values = (h % np.uint64(self.max_count)).astype(np.int64) + 1
        out = np.zeros(mask.shape, dtype=self.dtype)
        out[mask] = values[mask]
        return out

    def rows(self, i0: int, i1: int) -> sp.csr_matrix:
        """Rows i0..i1 as CSR (shape (i1 - i0, n_vars))."""
        step = max(1, _BLOCK_CELLS // max(self.n_vars, 1))
        parts = [sp.csr_matrix(self._dense_rows(s, min(i1, s + step))) for s in range(i0, i1, step)]
        return sp.vstack(parts, format="csr") if len(parts) > 1 else parts[0]

    def cols(self, j0: int, j1: int) -> sp.csc_matrix:
        """Columns j0..j1 as CSC (shape (n_obs, j1 - j0))."""
        width = j1 - j0
        step = max(1, _BLOCK_CELLS // max(width, 1))
        parts = [
            sp.csc_matrix(self._dense_rows(s, min(self.n_obs, s + step), j0, j1))
            for s in range(0, self.n_obs, step)
        ]
        return sp.vstack(parts, format="csc") if len(parts) > 1 else parts[0]

    def row_blocks(self, rows_per_block: int) -> Iterator[sp.csr_matrix]:
        for s in range(0, self.n_obs, rows_per_block):
            yield self.rows(s, min(self.n_obs, s + rows_per_block))

    def col_blocks(self, cols_per_block: int) -> Iterator[sp.csc_matrix]:
        for s in range(0, self.n_vars, cols_per_block):
            yield self.cols(s, min(self.n_vars, s + cols_per_block))
