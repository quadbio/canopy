# dbx progress log

Plan: `../reports/10_implementation_plan.md` (approved 2026-10-09). Decisions: `docs/decisions/`.
Resume rule: read this file, then continue with the first unchecked step.

## Stage 1 steps

- [x] S0 Foundation + compatibility checks
- [x] S1 Inspector + synthetic generator
- [x] S2 Contract v0 — APPROVED 2026-10-10 (decisions in contract/SPEC.md §11)
- [ ] Contract v0.2 DRAFT (contract/drafts/) awaiting user decision (options A/B/C)
- [ ] S3 Crawler + catalog API
- [ ] S4 Vertical slice (loader, API, skeleton, file browser, embedding view) → demo
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
