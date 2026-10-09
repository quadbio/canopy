# 0003: Data contract v0.1

Date: 2026-10-09 · Status: **proposed** (awaiting user approval; blocks S5/S6)

Specification: `contract/SPEC.md`. Schema: `contract/schema/manifest.schema.json`.
Models and validator: `backend/dbx/contract/`. Reference store: `dbx contract example OUT`.

## Decisions

1. Converted stores are a strict profile of AnnData-zarr (tables) and OME-NGFF 0.5 (images,
   labels); only polygons, transcripts, lookups and statistics use dbx-defined zarr arrays.
2. X is always gene-major CSC float32 (int32 indices, int64 indptr), 1 MiB chunks, zstd 3.
3. Images/labels: 512 px tiles, pyramids down to ≤512 px; mean for images, nearest for labels;
   labels are uint32 with an int32 label → table-row lookup.
4. Transcripts: sorted by feature, then Morton order; offsets per feature; a ≤1 M-point overview.
5. Every spatial element has one 3×3 affine per coordinate system, composed by the converter.
6. Arrays the browser reads directly are numeric; names go into JSON attributes (zarrita 0.5.4
   cannot decode zarr v3 variable-length strings: found while testing, see below).
7. Consolidated metadata per table and at the root, written after all arrays; stale metadata is
   a validation error (anndata reads through it).
8. Semantic versioning; store folder named by major version (`c0/`).

## Evidence (S2)

- The reference store passes `dbx validate --deep`; anndata 0.13 reads its table (CSC float32,
  colours, gene statistics) back unchanged.
- `frontend/scripts/read-store.mjs` reads it over HTTP with Viv 0.23 `loadOmeZarr` and zarrita
  0.5.4: image pyramid (OME 0.5 under `attrs.ome`, zstd, 512 px tiles incl. partial edge tiles),
  uint32 labels, lookup, polygons, points offsets and coordinates, X indptr (int64 → BigInt64Array).
- zarrita 0.5.4 throws `Unknown or unsupported data_type: string` on zarr v3 string arrays →
  decision 6.
- Validator tests: 13 corruption cases detected (missing manifest, wrong major, bad affine,
  undeclared coordinate system, CSR X, stale consolidated metadata, colour count, unsorted
  points, lookup length, pyramid shape, polygon starts, unknown manifest field).

## Not yet verified

- Stock Vitessce opening a converted table (planned in S5 with the first real converted store).
- Sharding (allowed by the spec for very large stores) is not exercised yet.
