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

(none yet; first runs in S5)
