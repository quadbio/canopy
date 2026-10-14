# 0002: Dataset fingerprints, privacy levels and mocks

Date: 2026-10-09 · Status: accepted (S1)

## Context

Claude never sees the host or the real data. Problems found on the host must be described by
what the logs carry and reproduced here. The crawler, loaders and converter also need to know a
file's structure before reading it.

## Decision

1. **Fingerprint** (`canopy.inspect`, `fingerprint_version` 1): a JSON description of a dataset's
   structure, read from metadata only (HDF5/zarr attributes, array shapes/dtypes/chunks/codecs,
   parquet footers). **It never contains values**: no expression values, barcodes, coordinates,
   category labels or gene names.
   - h5ad, AnnData-zarr: X/layers/raw/obsp encodings (dense/CSR/CSC), dtypes, nnz, chunking,
     compression; obs/var columns (kind, dtype, number of categories, colours present); obsm/varm
     shapes; uns keys; `uns[*_colors]` lengths.
   - SpatialData: images/labels (axes, pyramid levels, chunks, dtype, channel names, transforms),
     points/shapes (row counts, columns and arrow types, feature/instance keys, geometry types,
     transforms), tables (as AnnData + region/region_key/instance_key), coordinate systems.
   - `dataset_id` = hash(real path, mtime of the file or the store's root metadata). Since
     2026-10-14 (owner, host test 3) a dataset of the index uses its data root's name and path in
     the root instead of the real path (ADR 0012, revision).
   - `warnings`: structural facts that change serving (CSR matrix, non-float X, no pyramid,
     single-chunk image, points without feature_key).
2. **Privacy levels** (`canopy.inspect.redact`): `standard` keeps names; `strict` replaces every
   user-chosen name (path, file and element names, columns, keys, channels, coordinate systems)
   with a salted hash token, keeping format-defined names (`global`, `x`, `y`, `X_umap`, …).
   Tokens are stable for a given salt, so bundles from one installation stay comparable.
   *Revised 2026-10-10 (code review):* the salt is a random per-installation secret kept on the
   host (a public salt let a word list reverse common names); unknown levels are errors; a test
   checks that no user-chosen name from the standard fingerprint survives in strict output.
   Warnings are structured (`code`, `message`, `kind`, `element`) so names are never embedded in
   free text, and unreadable elements are recorded in `errors` instead of aborting.
   Fingerprint version 2 adds these, `layout` (anndata `current` / `0.7` / `pre-0.7`) and an
   optional `size_bytes`.
3. **Synthetic data** (`canopy.synth`): `SynthMatrix` defines a matrix by hashing (row, column), so
   the same values can be streamed in row blocks (CSR, dense) or column blocks (CSC). Files of
   any size are written with bounded memory (~1.4 GB peak at 1 M × 2 k).
4. **Mocks**: `write_mock(fingerprint)` rebuilds a look-alike dataset with random values and the
   same structure, scaled down. `structure_signature()` defines what must match; tests check the
   round trip fingerprint → mock → fingerprint on all example datasets.

## Consequences

- The fingerprint format is versioned; extending it means bumping `fingerprint_version` and
  keeping old bundles readable.
- The examples showed: SpatialData images stored as one chunk without a pyramid (converter must
  build pyramids), points without a declared `feature_key` (converter must pick the feature
  column, e.g. the dictionary-typed `gene_id`), multiple coordinate systems (`global`, `rot`),
  int64 dense tables, and anndata 0.13 writing string indexes as `nullable-string-array`.
