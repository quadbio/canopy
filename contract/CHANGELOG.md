# Data contract changelog

## 0.1.0 (approved 2026-10-10)

Review decisions: X = source X by default (layer choice optional); all obsm embeddings copied
(any number of dimensions ≥ 2); points gain `z_range` / `z_planes` for a 2-D display with a
z filter; holes dropped. Only one matrix copied; raw, other layers and graphs not copied.

## 0.1.0 (draft, 2026-10-09)

First version: tables (AnnData-zarr, CSC X, gene statistics, downsampling), images and labels
(OME-NGFF 0.5 pyramids, 512 px tiles), label lookups, shapes (polygons, circles), points
(feature-sorted, Morton-ordered, offsets, overview), manifest with 3x3 affine transforms.
