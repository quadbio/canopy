# Canopy progress log

Plan: `../reports/10_implementation_plan.md` (approved 2026-10-09). Decisions: `docs/decisions/`.
Resume rule: read this file, then continue with the first unchecked step.

## Stage 1 steps

- [x] S0 Foundation + compatibility checks
- [x] S1 Inspector + synthetic generator
- [x] S2 Contract v0 — APPROVED 2026-10-10 (decisions in contract/SPEC.md §11)
- [x] Contract v0.2 APPROVED (option A) and implemented in the contract code (2026-10-10)
- [x] S3 Crawler + catalog API (+ review batch 3: logging for several processes)
- [x] S4 Vertical slice (loader, API, skeleton, file browser, embedding view) → demo (awaiting user review)
- [x] S5 Genes + AnnData converter + jobs + memory guard
- [x] S6 Spatial ingest + spatial view + section grid → demo
- [ ] S7 Workspace + colours + search + filters (started: plot view done, ADR 0013)
- [ ] S8 Diagnostics tools → user checks bundles against the data policy
- [ ] S9 Packaging + runbook + Linux dry run → Checkpoint A

## Log

- 2026-10-09 S0 done.
  - Repo `canopy/` (git, branch main). pixi workspace: default/dev/frontend envs; `pixi.lock` for
    osx-arm64 + linux-64. Resolved: Python 3.14.8, anndata 0.13.4, zarr 3.4.1, spatialdata 0.8.0,
    fastapi 0.143, node 26, pnpm 12. Frontend: React 19.3, deck.gl 9.4.0, luma.gl 9.4.2, Viv 0.23.0,
    zarrita 0.5.4, Vite 7, vitest 3.
  - Backend: config (TOML, pydantic), structlog JSON logging (rotating file, request ids,
    `timed()` stages, memory sampler incl. cgroup v2), FastAPI app (/api/health, /api/version,
    serves frontend/dist), CLI (`canopy serve`, `canopy env`). 7 tests.
  - Frontend: smoke screen (backend health + one deck.gl canvas with two views), API client + 2 tests.
  - Checks: `pixi run -e dev check`; pre-commit hook via `pixi run -e dev install-hooks`.
  - Compatibility results in docs/decisions/0001: contract will use zarr v3 + OME-NGFF 0.5.
  - Next: S1 (inspector + synthetic generator).
- 2026-10-09 S1 done.
  - `canopy.inspect`: fingerprint (structure only) for h5ad, AnnData-zarr, SpatialData; detection;
    warnings; strict/standard redaction. Example timings: 9–75 ms per dataset.
  - `canopy.synth`: deterministic hash-defined matrix (identical CSR/CSC/dense), streaming AnnData
    writer (h5ad/zarr, chunks, compression, layers, colours), SpatialData writer (images with
    pyramids, labels, points, shapes, tables, transforms), mock from fingerprint +
    `structure_signature`. Round trip matches on all 3 example datasets.
  - CLI: `canopy inspect PATH [--level strict]`, `canopy synth anndata …`, `canopy synth mock FP OUT`.
  - Bench fixtures: `pixi run -e dev synth-bench` (1 M × 2 k CSR/CSC, ~53 s each, 1.4 GB RSS).
  - 26 backend tests (6 s). Decision record 0002. Example SpatialData archives are extracted
    to `.cache/examples/` by the tests.
  - Disk note: this Mac has ~21 GB free; keep bench fixtures ≤ ~5 GB total.
  - Next: S2 contract v0 → needs user approval.
- 2026-10-09 S2 drafted (awaiting user approval).
  - contract/SPEC.md (v0.1), contract/schema/manifest.schema.json (generated:
    `pixi run -e dev contract-schema`), contract/CHANGELOG.md, decision record 0003.
  - backend/canopy/contract: models (manifest), validate (`canopy validate STORE [--deep]`),
    write (array/pyramid/Morton helpers for the converter), example (`canopy contract example OUT`).
  - JS check: frontend/scripts/read-store.mjs (Viv + zarrita read the reference store).
    Finding: zarrita 0.5.4 cannot read zarr v3 string arrays -> names stored as attributes.
  - frontend: added zarrita ~0.5.4 as a direct dependency (same version as Viv's).
  - 43 backend tests.
  - Next after approval: S3 crawler + catalog (S3/S4 do not depend on the contract; S5/S6 do).
- 2026-10-10 S2 APPROVED by user, with decisions (SPEC §11):
  X = source X by default (layer choice optional, never mandatory); one matrix only (no raw,
  other layers, graphs); ALL obsm embeddings copied, browser picks embedding + 2 dims;
  spatial display 2-D, points keep z → z-plane / z-range filter (manifest z_range, z_planes);
  holes dropped. Validator + example + 2 tests updated (45 backend tests).
  - Next: cold code review by 2 subagents (only reports/09 + reports/10 as context) →
    reviews/code_review_S0-S2.md; then adapt; then S3.
- 2026-10-10 Cold review results in ../reviews/code_review_S0-S2/SUMMARY.md (finding IDs C*, I*,
  P*, S*). Fix batches 1–4 planned; contract v0.2 awaits user sign-off.
- 2026-10-10 Review batch 1 DONE (a687539): config strict + path checks + CANOPY_CONFIG; CLI logs
  never in cwd, --config everywhere; contract example safe; legacy h5ad (pre-0.7, 0.7);
  fixed-length strings; per-element errors; SpatialData w/o root attrs; HDF5 locking=False;
  dataset_id with size/store metadata; optional size; secret per-install salt, validated level,
  structured Notice warnings, no-leak test; fingerprint v2. 71 backend tests.
  Note: inspector reads root consolidated metadata when present (fast, one read).
  - Next: batch 2 = contract v0.2 draft (SUMMARY C1–C10, validator rules, JS check) → user sign-off.
- 2026-10-10 Contract v0.2 drafted: contract/drafts/SPEC-v0.2-draft.md + v0.2-comparison.md;
  bench/contract_v02_points.py (region_1: median gene 6.9 MB -> 0.11 MB with 16,384-row chunks;
  zoomed-out 39 MB -> 6.7 MB). Options A (approve all; spatial parts implemented at S6),
  B (cross-cutting now, spatial decided at S6), C (keep v0.1). Awaiting user decision.
- 2026-10-10 Contract v0.2 APPROVED (option A) and implemented: SPEC.md v0.2, CHANGELOG, ADR 0004,
  history/v0.1-to-v0.2-comparison.md; models (reader ignores unknown fields), validator
  (compat policy, layout/codec/shard rules, canonical CSC, root consolidation, names, polygons
  closed+CCW+Morton, points order/chunk_bbox/overview/lod, labels relabel/image-label; chunked
  deep checks), writer helpers for S5/S6 (sharded, polygon_layout, points_layout, relabel),
  example store v0.2; JS check automated (`check-store-js`, in `check`). 91 backend tests.
  Findings: @zarrita/storage 0.1.4 suffix-read bug -> use useSuffixRequest; Viv probes v2 (2x404).
  Still open from review S8: JS check against `canopy serve` (needs S3/S4 store serving),
  Playwright + check-all (S4), examples pytest marker. Batch 3 (logging for workers) before S5.
  - Next: S3 crawler + catalog (batch 3 alongside S3/S4).
- 2026-10-10 S3 DONE with review batch 3 (ADR 0005).
  - `canopy.catalog`: scandir walk (h5ad + AnnData/SpatialData zarr, never inside stores, hidden and
    `catalog.ignore` skipped, symlinks leaving a root skipped, loop-safe, max_depth), SQLite index
    in `<cache>/catalog.sqlite`, incremental rescans by version stamp, threaded inspection
    (`inspect_workers`), states inspected/unsupported/failed with reason codes, unavailable roots
    keep entries. Server: scan at start-up, every `rescan_minutes`, `POST /api/catalog/refresh`.
    API: roots, tree, search, datasets/{id} (fingerprint with root-relative path), status.
    `Catalog.resolve()` re-checks the root at open time (for S4 loaders). CLI `canopy catalog scan`.
  - Batch 3: `canopy.privacy` (salt, tokens, free-text redaction); per-process log files
    `canopy-<role>-<pid>.jsonl` with pid/role/build on every line, retention pruning; exceptions as
    type + code locations + redacted message; warnings captured; strict tokenises name fields;
    nested `timed()` with sampled peak RSS; request failures -> 500 JSON with request id, route
    template logged, bounded request ids; `canopy --version`, `x-canopy-build`; env adds HDF5 version,
    locking, available memory, filesystem type per root/cache.
  - `pixi run -e dev test-examples` (CANOPY_REQUIRE_EXAMPLES=1). 124 backend tests.
  - Measured: examples (4 datasets) indexed in 130 ms; 10k-file tree (300 datasets) in tests.
    Live strict-mode server log checked: no names or paths.
  - Next: S4 vertical slice. First task: adopt reviewer B's S4 kickoff checklist (SUMMARY 2.4
    note); also S6/S7 review items (SPA fallback, gzip, v3-only opens), Playwright + check-all,
    JS read check against `canopy serve`.
- 2026-10-10 S4 DONE (ADR 0006), demo ready for the user.
  - Backend: `canopy.data` (element-wise preview reads for h5ad / AnnData-zarr / SpatialData tables,
    legacy layouts loaded whole below small_file_gb, LRU cache for files + arrays with idle
    timeout), dataset API (meta, obs, obsm as binary + ETag), `/api/stores` with Range (single,
    suffix, clamped; hidden keys 404), gzip for /api, SPA fallback, `/api/telemetry`.
    `check-store-js` now runs against a real `canopy serve`.
  - Frontend (reviewer B checklist): api/ state/ views/ ui/ colour/ telemetry/ styles/; zustand,
    TanStack Query, Radix, tokens light/dark, colour Web Worker, view registry, one Deck with a
    grid of OrthographicViews; file browser (tree, search, details, Open); embedding view
    (embedding + dims, colour by category/number, legend with show/hide, size, opacity, tooltip).
  - Playwright e2e (`pixi run -e dev e2e`, screenshots in frontend/test-results/screens/);
    `check-all`. 155 backend tests, 11 frontend unit tests, 3 e2e tests.
  - Next: user reviews the demo; then S5 (gene expression, converter, jobs, memory guard,
    downsampling).
- 2026-10-10 Cold code review S3–S4 (2 subagents; ../reviews/code_review_S3-S4/: reviewer_A,
  reviewer_B, SUMMARY). A: S3 7, S4 backend 6.5, logging 6.5; B: F9 6.5, F10 7, F11a 7, F7 7.5.
  1 blocker, 13 major. All accepted. Batch R1 DONE (list in SUMMARY §4): Range streaming, hang
  visibility (slow_request, scan_progress, SIGUSR1 stacks), strict leaks closed + end-to-end strict
  test, truncated/not_anndata reasons, error envelope + codes in logs, no gzip on floats, catalog
  keeps entries on transient errors, string labels endpoint, nullable bools; frontend: validated
  workspace restore + error boundaries + Close all, stable GPU attributes (uploads tested), hidden
  not hovered, zoom kept, new palette, legend layout, version-keyed queries, view auto-registration.
  Also added: demo launcher + guide (docs/demo), architecture map (docs/architecture, artifact).
  176 backend tests, 19 frontend unit tests, 7 e2e tests.
  - Planned: R2 (GPU colour module, name-keyed colours) as the first task of S5; R3 (multi-panel
    view contract, per-type view options) before S6.
  - Next: user tries the demo (docs/demo/S4-demo-guide.md) and sends feedback; then S5.

- 2026-10-11 S4 demo feedback (docs/demo/feedback-S4.md): all 16 steps worked, no correctness
  issue. Batch F1 (triage: docs/demo/S4-feedback-triage.md): `?` help (Radix Tooltip) for Table
  and Embedding; table hint (rows, annotated region); dataset notes in plain language with
  take-home (ui/notes.tsx); guide steps 11/12/14 reworded; `CANOPY_DEMO_LARGE=1` adds synthetic 1M/3M
  h5ad; synth embeddings clustered by the first categorical column. 8 e2e tests.
  - Measured (Chrome, M2 GPU via ANGLE Metal): 1M/3M open -> ready 0.45/0.41 s; idle 58 fps;
    3M zoomed out ~60 ms/frame at 1440x900 (144 ms at 720x450: overdraw), zoomed in ~18 ms.
    Headless software GL is not representative (3M first draw 49 s).
  - Planned: R2 now also covers palette choice for categories and more colour maps; S5 subset
    drawing when zoomed out (frame budget target: 3M cells, zoomed out, < 20 ms).
  - Open question to user: remember last view settings per dataset on re-open?
  - Next: S5, starting with R2.
- 2026-10-11 Batch F2 (user said yes): remembered view settings per dataset/table/view type
  (workspace `remembered`, max 200, validated through `normalise` on recall), "Reset to defaults"
  in the settings panel; default point size 0.8 px above 1 M cells (user: bigger points are
  slower, as expected from fill cost). Storage falls back to memory when localStorage is missing.
  23 frontend unit tests, 9 e2e tests. Next: S5, starting with R2.
- 2026-10-11 S5 started. R2 DONE (ADR 0007): GPU colouring (ValueColourExtension: one float per
  point + RGBA lookup texture; palette/map/range/hidden change no point data), colour worker
  removed; colours by name (workspace labelSlots, slots.ts); palettes auto/dataset/default/
  colour-blind/many and 8 colour maps (d3-scale-chromatic, diverging centred on 0); colorBy is
  {kind, name}. Found and fixed: extension attribute needed stepMode 'dynamic' (all points read
  the first values); e2e now counts colours on screen. 30 frontend unit tests, 10 e2e tests.
  3M cells: ready 0.67 s, switch to numbers 0.4 s.
  - Next in S5: F7 gene endpoint (CSC direct, quantised) + gene colouring; then converter/jobs/
    memory guard; subset drawing when zoomed out.
- 2026-10-11 S5 gene expression DONE (ADR 0008). Backend: canopy.data.genes (access per matrix:
  direct CSC/dense, memory = CSR loaded once as CSC within small_file_gb and half the cache,
  needs_conversion above), /genes (names, symbols for Ensembl-like indexes, matrices) and /gene
  (sparse/dense, uint16 option, min/max/p99 headers). Frontend: gene search (combobox), Values
  from (X/layers), gene colouring with p99 scale, higher values drawn in front (shader). Fixed:
  front-shift pushed points past the near plane (e2e coverage check added, verified to fail on
  the bug). 190 backend tests, 38 frontend unit tests, 11 e2e tests. 3M cells: first gene 0.5 s,
  then 50–200 ms per gene switch.
  - Next: cold code review round (S5 part 1, R2, F1/F2) with two subagents; then converter +
    jobs + memory guard, subset drawing.
- 2026-10-11 Cold code review S5a (2 subagents; ../reviews/code_review_S5a/). A: gene backend 6.5,
  tests 6, demo backend 8; B: GPU colour 7, colours by name 5, gene UX 6.5, demo changes 7.5.
  0 blocker, 10 major. All accepted; batch R4 DONE (SUMMARY §4): chunk-aware dense access,
  single-flight + memory reservation (503 memory_busy), block-wise float32 CSR->CSC (radix sort;
  300k x 20k first gene 19.5 s -> 8.6 s, peak 2.14 -> 1.65 GB), gene log lines, exact
  quantisation, unique gene names, errno in logs; frontend: hash-based colours by name, never-blank
  views with Retry, depth only on the points layer (option), stronger e2e, faster gene search,
  safe throttled storage, help popover. bench/genes.py. 206 backend, 41 frontend unit, 14 e2e.
  - Deferred: subset drawing (S5 next), shared colour state (R3), colour range + zero grey (S7).
  - Next: S5 converter + jobs + memory guard (reuse block-wise build and reservation), then
    subset drawing.
- 2026-10-11 User rule: a column's uns['<col>_colors'] is the default palette whenever present.
  Already the "Automatic" behaviour; widened what counts as usable: colour names and #rgb via
  matplotlib, longer lists cut to the categories (as scanpy), colours for plain text columns
  (with /obs/labels); unusable lists logged as uns_colors_ignored with the reason. All example
  datasets' colour lists (15 columns) are used. 208 backend tests, 14 e2e.
  - Note for the converter (S5): SPEC 4.3 copies `<col>_colors`; apply the same rules there.
- 2026-10-11 S5 converter + jobs + memory guard (IN PROGRESS, committed, all tests pass: 222 backend,
  15 e2e). canopy.convert (out-of-core CSR/CSC/dense -> contract CSC via per-gene-bucket spill files,
  gene stats, stratified downsample, colours, validate, atomic publish), canopy.jobs (SQLite jobs,
  worker processes, cancel via SIGTERM, interrupted on restart, memory guard 1.5x jobs.memory_gb
  free, RSS watchdog), API /api/datasets/{id}/convert, /api/jobs; converted stores served
  (mode "converted", genes direct); UI Convert button + progress + cancel/retry.
  - Measured (M2): 3 M x 300 CSR (18 M values) 12 s, peak 1.32 GB; 300 k x 20 k CSR (120 M values)
    44 s, peak 2.02 GB with a 1 GB budget -> peak exceeds budget ~2x: tune block sizing (TODO).
  - TODO next: fix peak vs budget, ADR 0009 + README/guide docs, jobs config in demo, then subset
    drawing (B-M4); then S5 review round if wanted.
- 2026-10-11 S5 DONE. Converter memory fixed (radix sort, 12-byte spill records; 300k x 20k CSR
  in 22 s, peak 1.43 GB at 1 GB budget). ADR 0009 (converter, jobs, memory guard). Level of detail
  (B-M4): /subset endpoint, views/lod.ts, >1.5 M cells draw 1 M representative cells zoomed out
  (3 M: 18 ms/frame vs 56-66 before), "Show all cells". Demo guide docs/demo/S5-demo-guide.md +
  feedback-S5.md. 224 backend, 43 frontend unit, 15 e2e.
  - Next: user tries the S5 demo; optional review round on S5 part 2 (converter/jobs/LOD); then
    R3 (multi-panel view contract) and S6 (spatial).
- 2026-10-11 Cold review S5b (../reviews/code_review_S5b/): A converter 7, jobs 5, serving 6; B LOD 5,
  conversion UX 5, colours 8; 1 blocker, 15 major. All accepted; batch R5 DONE (SUMMARY §4): job
  robustness (leftover cleanup by job id, disk check, heartbeats, re-queue on server stop, one
  server per cache, one store per version + atomic swap, layers kept), memory guard with obs
  estimate + cgroup, richer worker logs, strict-safe, faithfulness fixes, canopy cache prune;
  frontend: server-truth conversion state + app-wide job watcher, LOD as prefix of a representative
  order with a canvas budget (3M: 18 ms zoomed out; 4 views 21 ms vs 72). 234 backend, 46 unit,
  16 e2e.
  - Next: user tries the S5 demo; then R3 (multi-panel view contract) and S6.
- 2026-10-11 S5 demo feedback (docs/demo/feedback-S5.md): 9/10 steps worked, no scientific issue,
  smooth. Batch F3 DONE (triage docs/demo/S5-feedback-triage.md): palettes extended by OKLCH
  midpoints instead of repeating (no duplicate colours); colours picked by hand in the legend
  (per column/category, remembered, reset); generated example_with_layers.h5ad (counts layer + log
  X, marker genes, 80-category column; `canopy synth demo`, new root "generated" in demo + e2e);
  Layers row in details. 235 backend, 49 unit, 17 e2e.
  - Proposed (awaiting user choice): Plot view for two features (scatter/density, violin, stacked
    bars; server-side summaries), ~4-6 days; recommended after S6 as start of S7.
  - Next: user's choice on plots; then R3 (multi-panel view contract, allowing non-map views) and S6.
- 2026-10-11 User chose (a): Plot view after S6, as the start of S7. Work paused until the user's usage resets; next: R3 (allow non-map views), then S6.
- 2026-10-11 R3 DONE (ADR 0010): views may return panels (one deck.gl view each, ids
  `<view>/<panel>`), grid inside the view's cell left of an open legend, one legend per view, linked
  pan+zoom relative to each panel's fit, "same scale" option, flipY per output (spatial coords
  y-down), legend-aware fitting. Embedding "Split by" (categorical <=64 / boolean) as the first
  user: panels share the point budget and upload ~one copy. 51 panels: median frame 16.7 ms
  (same as 1 panel). 57 unit, 18 e2e.
  - Next: S6 spatial (SpatialData path, pyramids, polygons, transcripts, section grid on panels).
- 2026-10-11 S6 DONE (ADR 0011), in parts: S6a SpatialData converter (tables via write_table
  factored out of the AnnData converter; images tile by tile from dask; labels with lookup;
  polygons/circles; transcripts by gene + Morton with cell link; affines per system; jobs +
  serving of converted SpatialData tables). Shared colouring module (views/colouring.tsx).
  S6b spatial view (Viv image with channels/colour/contrast, cells filled/outlined coloured on the
  GPU, transcripts of up to 6 genes, coordinate systems; "Open spatial view"/"Open embedding").
  S6c reading in view (canvas publishes view areas; outline blocks in view above 4 M vertices;
  transcript chunks in view for genes above the cap). Section grid = embedding Split by (R3).
  MERSCOPE region: 10 s, 1.7 GB; synthetic 200 k cells + 5 M transcripts + 8k² image: 12 s.
  239 backend, 62 unit, 20 e2e. Demo: docs/demo/S6-demo-guide.md (+ feedback-S6.md).
  - Open: z-plane filter (no example with z), labels colouring (stretch), scale bar (units
    unknown), vectorised outline conversion if needed.
  - Next: user demo S6; optional cold review round; then S7 (plot view first, user's choice).
- 2026-10-12 Cold review S6 (../reviews/code_review_S6/): A converter 5, memory 3, serving/jobs/logs
  6; B panels 7, spatial view 5, colours 6; 4 blockers, 13 major. Batch R6 DONE (table per finding:
  ../reviews/code_review_S6/R6-fixes.md; ADR 0010/0011 revised): multi-region tables + region-aware
  cell link + section_keys; reader without tables; transcripts partition-wise into compact arrays;
  masks block-wise; shapes vectorised and repaired; NaN dropped, strict-JSON manifest; sharded
  large image levels; units; feature_key option (+ UI field); disk check; progress inside
  elements; estimates calibrated (bench/spatial_memory.py, all peaks below estimate); impossible
  jobs fail, waiting jobs do not block; strict-readable validator problems; aggregated store
  request logs; ranges no-store/no 304. Frontend: own store layer (no-store, retry, cached arrays
  and chunks, one request per shard for whole arrays, abort, counters), visible failures + Retry,
  draw-style ids, stable polygon data, block colours, in-view by chunk boxes, panel fit modes
  (same area / same scale / fill), robust panel bounds, fitKey refit, contrast range from data,
  gene colours kept, debounced colour input, roving legend focus, telemetry (deck_error codes,
  store_reads, frames, panels, spatial view_ready). B-M3 (count-dependent palette slots) kept by
  the owner's decision; B-m2 (picked colours shared across views) to S7. 247 backend, 69 unit,
  21 e2e.
  - Next: user demo S6 (guide updated: steps 5, 8, 9); then S7 starting with the plot view.
- 2026-10-12 S6 demo feedback (docs/demo/feedback-S6.md; triage docs/demo/S6-feedback-triage.md):
  fixed blank view after Split by -> None (re-drawn released layer objects; canvas now copies a
  returning layer); hover: not reproduced (GPU + Retina probes fine), picking radius 3 px, telemetry
  `hover`, "Show values on hover" toggle for every view (user's wish); note that SpatialData needs
  converting before the spatial view; memory estimate recalibrated (MERSCOPE 1.62 vs 1.36 GB
  measured, was 3.0). 247 backend, 69 unit, 22 e2e.
  - Next: NOT S7 yet. The user may propose a cold review of everything built so far.
- 2026-10-12 Hover fixed (user: still nothing on hover): demo logs showed values produced but invisible; deck.gl's tooltip sat off screen at devicePixelRatio 2. Canvas draws the hover text itself; e2e at 2x checks placement. 23 e2e.
  - Next: NOT S7; user may propose a full cold review.
- 2026-10-12 Full cold review (../reviews/code_review_full_1/): A backend 7.5/7/5.5/7/7, B frontend
  7.5/7/8/6.5/5.5/7, C host readiness 3/3/7/6/4/4; 2 blockers, 21 major. Owner: token+cookie, keep
  default colours, host readiness before S7, rename to Canopy (logo). Batch R7 DONE (table:
  ../reviews/code_review_full_1/R7-fixes.md; ADR 0012): rename; access token + private files +
  state_dir; strict leaks closed, ids linked, browser error details; canopy bundle, start-up
  records, token find; cgroup v1/hybrid/parent limits, OOM labels; one memory budget, adopted and
  silent workers, per-block final check, queue and store caps; release archive + host guide +
  host config; robustness (barcode columns, HDF5 locking, ESTALE retry, meta cache); frontend
  (spatial view without copy, uint16 embeddings, foldable panels, long legends, GPU 2x e2e).
  270 backend, 72 unit, 25 e2e (also 25 on GPU at 2x).
  - Open: Linux run (needs Docker/Podman or a Linux machine; scripts/linux-smoke.sh).
  - Next: the owner's first host test (docs/host/host-guide.md); S7 starts with the view
    contract for plots (ADR 0012).
- 2026-10-12 S7 started with the plot view (owner's choice; ADR 0013): server summaries
  (/summary: violins, count tables, densities; 10 M cells 0.1-1.5 s), view contract for charts
  (content, overlays, margins, panel sizes), Plot view (violins, composition, scatter with axes,
  density map; shared colours; log1p axes), "Plot" button in the details. 275 backend, 79 unit,
  26 e2e.
  - Next: rest of S7 (multi-dataset workspace and shared colours, gene search, filters) after the
    owner tries the plot view.
- 2026-10-12 Plot redesign (owner: plot bound to the opened view): "Plot against a second feature"
  (unticked by default) under Colour by in embedding and spatial settings opens a plot view
  beside it; x follows the view's Colour by, y chosen there; the view's colours, hidden
  categories and legend apply; unticking or closing the view closes the plot. Plot button
  removed (ADR 0013 revised, S7a guide and form rewritten). 276 backend, 81 unit, 26 e2e.
  Commit 20acd51. Cold review of 477c92f..20acd51 launched (../reviews/code_review_S7a/).
  - Next: report reviews; owner does the S7a demo review.
