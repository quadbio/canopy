# Benchmarks

Benchmark fixtures are generated, never committed: `pixi run -e dev synth-bench` writes them to
`.cache/bench/`. Results of benchmark runs are recorded here (newest first) with the commit,
machine and numbers, so changes can be compared across releases.

## Fixtures

| File | Shape | Density | Layout | Size | Generation (M2 Mac) | Peak RSS |
|---|---|---|---|---|---|---|
| `synth_1m_csr.h5ad` | 1,000,000 × 2,000 | 5 % (100 M values) | CSR, gzip | 0.56 GB | 52 s | 1.45 GB |
| `synth_1m_csc.h5ad` | same matrix | 5 % | CSC, gzip | 0.52 GB | 55 s | 1.43 GB |

Both files hold the identical matrix (`dbx.synth.SynthMatrix`, seed 0), so results from the two
layouts can be compared value for value. Generation streams X in blocks: peak memory does not grow
with file size, and 5 GB fixtures take proportionally longer (~5 min).

## Results

### 2026-10-11 · gene requests (`bench/genes.py`) · MacBook Pro M2, 16 GB, local SSD · S5 R4

Fixtures: `dbx synth anndata <f> --n-obs 300000 --n-vars 20000 --density 0.02` (120 M values,
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
