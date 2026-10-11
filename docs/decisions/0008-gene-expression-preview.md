# 0008 Gene expression on the preview path

Date: 2026-10-11 · Status: accepted (S5, first part)

## Context
F6/F7 of the plan: colour cells by a gene without converting the file first, where that is
cheap, and say clearly where it is not. One gene is a column of the expression matrix; how
expensive a column is depends on the layout: gene-major (CSC) and dense matrices give a column
directly, cell-major (CSR, the most common layout of h5ad files, and all example datasets) only
by reading the whole matrix (Report 03 measured this).

## Decisions
- **Access per matrix**, decided from the fingerprint (no file access): `direct` for CSC and
  dense; `memory` for CSR whose size is at most `memory.small_file_gb` and half of
  `memory.cache_gb`: the matrix is loaded once, converted to CSC in memory and cached in the
  dataset LRU like any array (logged as `load_array what=matrix_csc`); `needs_conversion` above
  that (409 `needs_conversion:x_csr`, explained in the UI). The converter (next S5 step) turns
  those into CSC stores.
- **Matrices:** `X` and every layer with one column per gene; the user picks "Values from".
  `raw` is not offered yet (its gene list can differ from `var`).
- **Gene list:** the var index is the gene's identity (requests use its position, saved
  settings its name); a symbol column is used for display and search only when the index looks
  like Ensembl ids.
- **Wire format:** sparse (uint32 indices + values) when smaller than dense; uint16
  quantisation on request (the browser always asks: half the bytes, 65,534 steps, finer than
  the 256-colour scale); statistics in headers (min, max, p99 of non-zero values). Not gzipped.
- **Colour scale:** 0 (or the minimum, if negative) to the 99th percentile of the expressing
  cells. **Higher values are drawn in front** (as scanpy does), in the vertex shader, by
  moving points part of the way towards the near plane — no re-sorting or re-uploading.
- `colorBy = {kind: 'gene', name, matrix?}`; a saved gene that is gone, or whose matrix needs
  conversion, is dropped with a notice.

## Evidence
- Golden tests against anndata: CSC / CSR / dense in h5ad and zarr, layers, both example h5ad
  files and a SpatialData table (X and `counts`); quantised values within half a step; 409 for
  a CSR matrix above the limit while a CSC file of the same size reads directly; the CSR matrix
  is loaded once for many genes.
- e2e: search → legend, colour gradient, tooltip value, all cells still drawn; layer choice.
- 3 M cells (synthetic CSR, M2, Chrome): first gene 0.5 s (matrix loaded once), then ~6 ms per
  gene on the server and 50–200 ms from picking a gene to the redrawn view (360 KB per gene).

## Lessons
- Moving points towards the viewer by a fixed amount pushed them past the near plane, so
  expressing cells vanished; the e2e test now checks that coverage does not drop when colouring
  by a gene, and was shown to fail on the old shader.
