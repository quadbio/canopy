# 0009 AnnData converter, job system and memory guard

Date: 2026-10-11 · Status: accepted (S5)

## Context
The preview path cannot serve gene expression from large cell-major (CSR) or badly chunked dense
matrices (ADR 0008). Plan F8: convert such files once into a dbx store (contract/SPEC.md §2–§4),
in the background, with progress, on a shared host with no guaranteed free memory, and debuggable
from logs alone.

## Decisions
- **Converter** (`dbx.convert`): h5ad and AnnData-zarr (current layout) → `<cache>/<version>/c0/`.
  - obs and var read whole; embeddings copied in row blocks (float32, 65,536-row chunks);
    `<col>_colors` with the preview's rules (names → hex, longer lists cut, unusable dropped with
    a warning), also for text columns.
  - **X out of core:** count entries per gene, then (CSR/dense sources) one pass over the cells
    spilling each entry to the temporary file of its *gene bucket* (consecutive genes whose
    entries fit the working memory; 12 bytes per entry on disk), then bucket by bucket: radix
    sort by gene (rows stay ascending → canonical CSC), write one contiguous range, compute gene
    statistics (nnz, mean, max, p99 of non-zero values). CSC sources skip the spill. Duplicate
    entries are refused (`duplicate_entries`).
  - The matrix chosen as X: `X` (default), a layer, or `raw/X` when X is missing (SPEC §4.2).
  - Downsampling (> 200 k cells): 200 k and 1 M cell indices, stratified by the first coloured
    categorical column (every group keeps at least one cell), fixed seed.
  - Built in `c0.tmp-*`, validated deeply (`dbx validate --deep` rules), then renamed; anything
    that fails or is stopped removes its temporary store and spill files.
- **Jobs** (`dbx.jobs`): a SQLite job table in the cache; each job runs in its own worker process
  (`python -m dbx.jobs.worker`), logging to `dbx-worker-<pid>.jsonl`. States queued → running →
  done | failed | cancelled; a job whose process vanished becomes `interrupted` at the next start.
  Cancel = SIGTERM, honoured at the next progress report. An identical active job is reused.
- **Memory guard:** a job starts only when the host has 1.5 × `jobs.memory_gb` available (else it
  waits: stage `waiting_for_memory`, logged); `jobs.max_running` (default 1) bounds concurrency;
  the worker's watchdog stops a job beyond 2 × `jobs.memory_gb` + 1 GB (`memory_limit`) before the
  system's out-of-memory killer would end it silently. The preview path's reservations (ADR 0008)
  guard the server process itself.
- **Serving:** once a store exists for a dataset version, the dataset is read from it (`mode:
  converted`; X is CSC: every gene direct). The source file stays the reference for the catalog.
- **UI:** "Convert for full speed" in the dataset details and in the Gene box when needed, with
  the stage, progress, Cancel and Try again; queries refresh when the job finishes.

## Evidence
- Tests: CSR/CSC/dense × h5ad/zarr with a tiny budget (many buckets and blocks) give stores that
  validate and equal the source (X, obs, var, embeddings, colours, statistics); a layer as X;
  duplicate entries refused; a stop removes everything; downsampling keeps every group; jobs end to
  end through a real worker (queue, reuse, done, served from the store), waiting for memory,
  cancel, interrupted after restart, failure reason in the worker's log; e2e conversion in the UI.
- M2 Mac: 3 M × 300 CSR (18 M values) in 12 s, peak 1.32 GB (1 GB budget); 300 k × 20 k CSR
  (120 M values, 0.44 GB gzip) in 21–22 s, peak 1.43 GB (1 GB budget) / 1.21 GB (0.5 GB budget).
  The peak is about the interpreter and libraries (0.25 GB) + 1.2 × budget + ~0.3 GB.

## Known limits
- SpatialData datasets are converted in S6 (images, labels, shapes, points, their tables).
- obs and var are read whole: tens of millions of cells with many text columns need several GB.
- Spill files need about 12 bytes per stored value in `jobs.tmp_dir` (default `<cache>/tmp`).
