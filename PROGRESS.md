# dbx progress log

Plan: `../reports/10_implementation_plan.md` (approved 2026-10-09). Decisions: `docs/decisions/`.
Resume rule: read this file, then continue with the first unchecked step.

## Stage 1 steps

- [x] S0 Foundation + compatibility checks
- [x] S1 Inspector + synthetic generator
- [x] S2 Contract v0 — APPROVED 2026-10-10 (decisions in contract/SPEC.md §11)
- [x] Contract v0.2 APPROVED (option A) and implemented in the contract code (2026-10-10)
- [x] S3 Crawler + catalog API (+ review batch 3: logging for several processes)
- [x] S4 Vertical slice (loader, API, skeleton, file browser, embedding view) → demo (awaiting user review)
- [ ] S5 Genes + AnnData converter + jobs + memory guard
- [ ] S6 Spatial ingest + spatial view + section grid → demo
- [ ] S7 Workspace + colours + search + filters
- [ ] S8 Diagnostics tools → user checks bundles against the data policy
- [ ] S9 Packaging + runbook + Linux dry run → Checkpoint A

## Log

- 2026-10-09 S0 done.
  - Repo `dbx/` (git, branch main). pixi workspace: default/dev/frontend envs; `pixi.lock` for
    osx-arm64 + linux-64. Resolved: Python 3.14.8, anndata 0.13.4, zarr 3.4.1, spatialdata 0.8.0,
    fastapi 0.143, node 26, pnpm 12. Frontend: React 19.3, deck.gl 9.4.0, luma.gl 9.4.2, Viv 0.23.0,
    zarrita 0.5.4, Vite 7, vitest 3.
  - Backend: config (TOML, pydantic), structlog JSON logging (rotating file, request ids,
    `timed()` stages, memory sampler incl. cgroup v2), FastAPI app (/api/health, /api/version,
    serves frontend/dist), CLI (`dbx serve`, `dbx env`). 7 tests.
  - Frontend: smoke screen (backend health + one deck.gl canvas with two views), API client + 2 tests.
  - Checks: `pixi run -e dev check`; pre-commit hook via `pixi run -e dev install-hooks`.
  - Compatibility results in docs/decisions/0001: contract will use zarr v3 + OME-NGFF 0.5.
  - Next: S1 (inspector + synthetic generator).
- 2026-10-09 S1 done.
  - `dbx.inspect`: fingerprint (structure only) for h5ad, AnnData-zarr, SpatialData; detection;
    warnings; strict/standard redaction. Example timings: 9–75 ms per dataset.
  - `dbx.synth`: deterministic hash-defined matrix (identical CSR/CSC/dense), streaming AnnData
    writer (h5ad/zarr, chunks, compression, layers, colours), SpatialData writer (images with
    pyramids, labels, points, shapes, tables, transforms), mock from fingerprint +
    `structure_signature`. Round trip matches on all 3 example datasets.
  - CLI: `dbx inspect PATH [--level strict]`, `dbx synth anndata …`, `dbx synth mock FP OUT`.
  - Bench fixtures: `pixi run -e dev synth-bench` (1 M × 2 k CSR/CSC, ~53 s each, 1.4 GB RSS).
  - 26 backend tests (6 s). Decision record 0002. Example SpatialData archives are extracted
    to `.cache/examples/` by the tests.
  - Disk note: this Mac has ~21 GB free; keep bench fixtures ≤ ~5 GB total.
  - Next: S2 contract v0 → needs user approval.
- 2026-10-09 S2 drafted (awaiting user approval).
  - contract/SPEC.md (v0.1), contract/schema/manifest.schema.json (generated:
    `pixi run -e dev contract-schema`), contract/CHANGELOG.md, decision record 0003.
  - backend/dbx/contract: models (manifest), validate (`dbx validate STORE [--deep]`),
    write (array/pyramid/Morton helpers for the converter), example (`dbx contract example OUT`).
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
- 2026-10-10 Review batch 1 DONE (a687539): config strict + path checks + DBX_CONFIG; CLI logs
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
  Still open from review S8: JS check against `dbx serve` (needs S3/S4 store serving),
  Playwright + check-all (S4), examples pytest marker. Batch 3 (logging for workers) before S5.
  - Next: S3 crawler + catalog (batch 3 alongside S3/S4).
- 2026-10-10 S3 DONE with review batch 3 (ADR 0005).
  - `dbx.catalog`: scandir walk (h5ad + AnnData/SpatialData zarr, never inside stores, hidden and
    `catalog.ignore` skipped, symlinks leaving a root skipped, loop-safe, max_depth), SQLite index
    in `<cache>/catalog.sqlite`, incremental rescans by version stamp, threaded inspection
    (`inspect_workers`), states inspected/unsupported/failed with reason codes, unavailable roots
    keep entries. Server: scan at start-up, every `rescan_minutes`, `POST /api/catalog/refresh`.
    API: roots, tree, search, datasets/{id} (fingerprint with root-relative path), status.
    `Catalog.resolve()` re-checks the root at open time (for S4 loaders). CLI `dbx catalog scan`.
  - Batch 3: `dbx.privacy` (salt, tokens, free-text redaction); per-process log files
    `dbx-<role>-<pid>.jsonl` with pid/role/build on every line, retention pruning; exceptions as
    type + code locations + redacted message; warnings captured; strict tokenises name fields;
    nested `timed()` with sampled peak RSS; request failures -> 500 JSON with request id, route
    template logged, bounded request ids; `dbx --version`, `x-dbx-build`; env adds HDF5 version,
    locking, available memory, filesystem type per root/cache.
  - `pixi run -e dev test-examples` (DBX_REQUIRE_EXAMPLES=1). 124 backend tests.
  - Measured: examples (4 datasets) indexed in 130 ms; 10k-file tree (300 datasets) in tests.
    Live strict-mode server log checked: no names or paths.
  - Next: S4 vertical slice. First task: adopt reviewer B's S4 kickoff checklist (SUMMARY 2.4
    note); also S6/S7 review items (SPA fallback, gzip, v3-only opens), Playwright + check-all,
    JS read check against `dbx serve`.
- 2026-10-10 S4 DONE (ADR 0006), demo ready for the user.
  - Backend: `dbx.data` (element-wise preview reads for h5ad / AnnData-zarr / SpatialData tables,
    legacy layouts loaded whole below small_file_gb, LRU cache for files + arrays with idle
    timeout), dataset API (meta, obs, obsm as binary + ETag), `/api/stores` with Range (single,
    suffix, clamped; hidden keys 404), gzip for /api, SPA fallback, `/api/telemetry`.
    `check-store-js` now runs against a real `dbx serve`.
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

