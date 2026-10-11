# 0008 Gene expression on the preview path

Date: 2026-10-11 · Status: accepted (S5, first part)

## Context
F6/F7 of the plan: colour cells by a gene without converting the file first, where that is
cheap, and say clearly where it is not. One gene is a column of the expression matrix; how
expensive a column is depends on the layout: gene-major (CSC) and dense matrices give a column
directly, cell-major (CSR, the most common layout of h5ad files, and all example datasets) only
by reading the whole matrix (Report 03 measured this).

## Decisions
- **Access per matrix**, decided from the fingerprint (no file access), by the bytes one gene
  touches on storage (`genes.column_read_bytes`):
  - CSC: `direct` (one contiguous slice per gene);
  - dense: `direct` only when a column touches at most 64 MB (narrow chunks); row-shaped chunks
    or contiguous storage would read the whole matrix per gene (review S5a A-M1);
  - otherwise `memory` when the in-memory float32 copy is at most `memory.small_file_gb` and
    half of `memory.cache_gb`, else `needs_conversion` (409 `needs_conversion:matrix_<layout>`).
- **The in-memory copy** (`memory` access) is built once per matrix and cached in the dataset LRU:
  CSR is turned into float32 CSC **block by block** (two passes over the indices: count per gene,
  then fill), so the peak is the copy plus about two blocks, never CSR and CSC together; dense
  input is read in row blocks into a column-major array. The dataset lock is released between
  blocks, so other requests on the dataset are not frozen. *(R4: A-M3, A-m3, A-m5)*
- **Single-flight and reservation** (`DatasetCache.array`): concurrent requests for the same
  array wait for one load; a load first reserves its estimated peak, evicting older cache
  entries, and is refused with 503 `memory_busy` when the cache plus reservations would exceed
  `memory.budget_gb` or the host has less memory available. This is the hook for the S5 memory
  guard and the converter. *(R4: A-M2, A-M3)*
- **Matrices:** `X` and every layer with one column per gene; the user picks "Values from" (the
  first readable one by default). `raw` is not offered yet (its gene list can differ from `var`).
- **Gene list:** the var index made unique the anndata way (`A`, `A-1`) is the gene's identity
  (requests use its position, saved settings its name); a symbol column is used for display and
  search when the index looks like database ids (Ensembl, FlyBase, WormBase, TAIR, Entrez).
- **Wire format:** sparse (uint32 indices + values) when smaller than dense; uint16 quantisation
  on request (the browser always asks): exact for integers within 65,534 of each other (counts),
  otherwise 0 always exact and every value within half a step; ±inf to the end codes. Headers:
  true min/max over all cells, p99 of non-zero values, number of expressing cells. Not gzipped.
- **Colour scale:** 0 (or the minimum, if negative) to the 99th percentile of the expressing
  cells. **Higher values are drawn in front** (as scanpy does) through the depth test of the
  points layer only (see ADR 0007).
- **Logs:** one `gene_served` line per request (matrix as a name field, gene index tokenised at
  `strict`, layout, access, cache hit/miss/waited for the gene and the matrix copy, q, encoding,
  cells, expressing cells, reply bytes, estimated bytes read); `gene_refused` for conversion
  cases; `memory_busy` with the numbers; errno kept on I/O errors at every privacy level.
- `colorBy = {kind: 'gene', name, matrix?}`; a saved gene that is gone, or whose matrix needs
  conversion, is dropped with a notice.

## Evidence
- Golden tests against anndata: CSC / CSR / dense in h5ad and zarr, layers, both example h5ad
  files and both SpatialData tables (X and `counts`, counts exact after quantisation); 409 for a
  CSR matrix above the limit while a CSC file reads directly; a row-chunked dense matrix is
  loaded whole once; 8 concurrent loads run once, and a failed load fails its waiters and can be
  retried; reservations evict or refuse; the block-wise build equals scipy's; duplicate names;
  quantisation of counts, zeros, negative values and ±inf; strict logs contain no gene or layer
  names.
- `bench/genes.py`, 300 k cells × 20 k genes (M2, SSD): CSC 3 ms per gene; CSR first gene 8.6 s
  (builds the 0.96 GB copy, peak RSS 1.65 GB), then 2–5 ms per gene; 8 parallel new genes in
  0.02 s. Details in `bench/README.md`. (The earlier "6 ms per gene at 3 M cells" came from a
  300-gene matrix.)
- e2e: search → legend, colour gradient (both colour-map ends on screen), tooltip value, all
  cells still drawn; layer choice; draw order; slow and failing genes.

## Lessons
- Moving points towards the viewer by a fixed amount pushed them past the near plane, so
  expressing cells vanished; the e2e test now checks that coverage does not drop when colouring
  by a gene, and was shown to fail on the old shader.
