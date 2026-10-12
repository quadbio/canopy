# Benchmarks

Benchmark fixtures are generated, never committed: `pixi run -e dev synth-bench` writes them to
`.cache/bench/`. Results of benchmark runs are recorded here (newest first) with the commit,
machine and numbers, so changes can be compared across releases.

## Fixtures

| File | Shape | Density | Layout | Size | Generation (M2 Mac) | Peak RSS |
|---|---|---|---|---|---|---|
| `synth_1m_csr.h5ad` | 1,000,000 × 2,000 | 5 % (100 M values) | CSR, gzip | 0.56 GB | 52 s | 1.45 GB |
| `synth_1m_csc.h5ad` | same matrix | 5 % | CSC, gzip | 0.52 GB | 55 s | 1.43 GB |

Both files hold the identical matrix (`canopy.synth.SynthMatrix`, seed 0), so results from the two
layouts can be compared value for value. Generation streams X in blocks: peak memory does not grow
with file size, and 5 GB fixtures take proportionally longer (~5 min).

## Results

### 2026-10-12 · SpatialData conversion memory (`bench/spatial_memory.py`) · MacBook Pro M2, 16 GB, local SSD · R6 + S6 feedback

One synthetic store per element kind (`--scale 1`, `--threads 4`), each converted in a fresh
process, plus the two MERSCOPE examples. Peak = resident memory above the interpreter baseline.
The estimate must stay at or above the peak for what is read whole; a table's matrix is covered
by the matrix budget instead (`jobs.memory_gb`, 2 GB here). Limit = the job watchdog's limit.
Estimate constants after the S6 feedback (the first calibration was 2.3× too high for the
MERSCOPE region, so its conversion could wait for memory on a laptop): 100 B per transcript, one
partition read at 5× its column bytes (strings 16 B), the cell column the converter links.

| case | seconds | peak GB | estimate GB | estimate / peak | limit GB |
|---|---|---|---|---|---|
| 10 M transcripts, x, y, z, gene, int cell id, qv, fov | 5.4 | 0.73 | 1.31 | 1.79 | 7.0 |
| 10 M transcripts, string cell and transcript ids | 5.3 | 0.73 | 1.31 | 1.79 | 7.0 |
| 12 k × 12 k int64 mask, 250 k sparse IDs (relabelled) | 4.4 | 2.00 | 2.75 | 1.38 | 9.1 |
| 2 × 12 k × 12 k uint16 image, 4 k chunks | 3.6 | 0.66 | 0.88 | 1.33 | 6.3 |
| the same image as one chunk | 4.4 | 1.10 | 2.93 | 2.66 | 9.4 |
| 300 k square polygons | 1.6 | 0.70 | 0.98 | 1.40 | 6.5 |
| table 400 k × 3 000, 60 M values (CSR) | 8.7 | 1.96 | 0.28 (+ 2 GB budget) | – | 5.4 |
| MERSCOPE region_1 (5.6 M transcripts, 3 images, 2 masks, 88 k outlines) | 8.4 | 1.36 | 1.62 | 1.19 | 7.4 |
| MERSCOPE region_3 | 8.0 | 1.39 | 1.68 | 1.21 | 7.5 |

Before R6 (review S6, reviewer A): the table's matrix was also loaded whole by the reader
(2.25 GB against a 0.04 GB estimate), transcripts took 90–250 B each against 48 estimated, masks
27 B/px against 16.


### 2026-10-11 · conversion (`canopy.convert`) · MacBook Pro M2, 16 GB, local SSD · S5

| source | values | budget | time | peak RSS |
|---|---|---|---|---|
| 3 M × 300 CSR (demo `synthetic_3M_cells.h5ad`) | 18 M | 1 GB | 12 s | 1.32 GB |
| 300 k × 20 k CSR (`--density 0.02`, 0.44 GB gzip) | 120 M | 1 GB | 22 s | 1.43 GB |
| same | 120 M | 0.5 GB | 21 s | 1.21 GB |

Stages for the 120 M-value file: counting 3 s, reordering into gene buckets 7 s, writing gene by
gene 11 s, deep validation 1 s.

### 2026-10-11 · gene requests (`bench/genes.py`) · MacBook Pro M2, 16 GB, local SSD · S5 R4

Fixtures: `canopy synth anndata <f> --n-obs 300000 --n-vars 20000 --density 0.02` (120 M values,
0.96 GB as a float32 CSC copy), once `--encoding csr`, once `--encoding csc` (gzip, ~0.43 GB
each). Requests with `q=16`; 20 genes after the first, then 8 new genes in parallel.

| file | layout / access | first gene | next genes (median, max) | 8 parallel new genes (wall) | KB per gene | peak RSS MB |
|---|---|---|---|---|---|---|
| csc_300k_20k.h5ad | csc / direct | 0.00 s | 3 ms, 4 ms | 0.02 s | 35 | 232 |
| csr_300k_20k.h5ad | csr / memory | 8.62 s | 2 ms, 5 ms | 0.02 s | 35 | 1651 |

The first CSR gene builds the in-memory CSC copy block by block: about 4.7 s reading and
decompressing (indices twice, values once), the rest grouping by gene. Before the radix-sort
change it took 19.5 s and peaked at 2.14 GB. On network storage expect the reading part to grow
with the link speed; CSC files are unaffected.
