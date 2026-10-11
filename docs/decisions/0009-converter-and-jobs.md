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

- **Robustness (R5, review S5b):**
  - **Temporary folders** carry the job id (`c0.tmp-j<job>-…`); those of jobs that are not
    running are removed at start, before each launch and after a worker dies (also after a kill
    that left no chance to clean up).
  - **Free disk** is checked first: 12 bytes per value for spill files, 8 for the store, +10 %;
    otherwise `not_enough_disk`.
  - **Workers** write a heartbeat every 5 s. A running job whose process is gone, whose pid now
    belongs to another process (start time checked), or that is silent for 2 min becomes
    `interrupted`. A job stopped by a server shutdown is re-queued at the next start.
  - **One server per cache folder** (`server.lock`); each worker gets its own config file.
  - **One store per dataset version:** converting again needs `force` (409
    `already_converted`), one active job per dataset (409 `conversion_running`), and a
    replaced store is renamed aside before the new one moves in. The server describes a store
    by its manifest's time, so a re-converted or deleted store is noticed.
  - **Other layers stay available** after conversion, served from the source as before.
  - **The memory guard and watchdog** include an estimate of obs/var (read whole): about 80
    bytes per cell plus 4–64 bytes per column value.
  - **Logs:** sub-stage durations, buckets, spill bytes and free disk; a progress line every
    30 s with RSS; memory samples; `version` for the dataset version. Layer names and
    validator details are name fields (tokenised at strict).
  - **Faithfulness:** strings stay strings; raw/X always takes raw's genes; NaN/inf counts and
    integers above 2²⁴ are reported as warnings; a file changed since its inspection or during
    the conversion is refused (`source_changed`). Downsampling works with more categories than
    cells to keep and stratifies by the main annotation (cell type / cluster first).
  - **`dbx cache prune`** removes stores of versions the catalog no longer knows, and leftovers.
- **Sizing for atlases:** `jobs.memory_gb` sets the matrix working memory; a job needs about
  0.3 GB + 1.2–1.5 × `memory_gb` + the obs/var estimate. For 10⁷–10⁸ cells use 16–64 GB: fewer,
  larger buckets and row blocks (each block appends to every bucket; appends are buffered up to
  64 MB).

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
- Not built from plan F8: resumable stages (a stopped job starts again from the beginning) and
  the "one large + several small jobs" rule (`max_running` bounds all jobs alike). The 1–5 GB
  synthetic layout tests of the S5 row are covered by `bench/convert.py` and `bench/genes.py`
  runs recorded in `bench/README.md`, not by automated tests.
- X is not sharded: stores above about 100,000 chunk files (≈ 2.6 × 10¹⁰ values) need the
  sharding the SPEC allows; it comes with the S6 writers.
- SpatialData datasets are converted in S6 (images, labels, shapes, points, their tables).
- obs and var are read whole: tens of millions of cells with many text columns need several GB.
- Spill files need about 12 bytes per stored value in `jobs.tmp_dir` (default `<cache>/tmp`).
