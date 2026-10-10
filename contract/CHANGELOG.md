# Data contract changelog

## 0.2.0 (approved 2026-10-10)

From the cold code review (comparison: `history/v0.1-to-v0.2-comparison.md`).
- Compatibility: readers ignore unknown fields; newer minor versions open with a warning;
  `dbx validate` strict for its own version (§9). Validator rules listed in §12.
- Points: 16,384-row chunks in shards of 64; `feature` array dropped; `offsets` uint32;
  `chunk_bbox`; materialised `overview/`; per-gene `lod/` above 100,000 points.
- Polygons: closed counter-clockwise rings; `starts` uint32; Morton order of centroids;
  `centroid`, `block_bbox`; `n_vertices`.
- Labels: relabelling of 64-bit or sparse IDs (`original_id`, `relabelled`, `n_labels`);
  negative values are background; NGFF `image-label` metadata and `labels/` listing.
- Tables: `section_keys`; obsm chunking validated; downsample dtype clarified.
- Manifest: `units` per coordinate system; element names and paths validated.
- Server: HTTP Range incl. suffix ranges required.

## 0.1.0 (approved 2026-10-10)

Review decisions: X = source X by default (layer choice optional); all obsm embeddings copied
(any number of dimensions ≥ 2); points gain `z_range` / `z_planes` for a 2-D display with a
z filter; holes dropped. Only one matrix copied; raw, other layers and graphs not copied.

## 0.1.0 (draft, 2026-10-09)

First version: tables (AnnData-zarr, CSC X, gene statistics, downsampling), images and labels
(OME-NGFF 0.5 pyramids, 512 px tiles), label lookups, shapes (polygons, circles), points
(feature-sorted, Morton-ordered, offsets, overview), manifest with 3x3 affine transforms.
