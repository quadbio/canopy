# 0011 SpatialData conversion and the spatial view (S6)

Date: 2026-10-11 · Status: accepted (S6)

## Context
Plan F8/F11b/F11c: SpatialData stores (images, segmentation, cell outlines, transcripts, tables)
must be browsable at full speed. Contract v0.2 (SPEC §5–§8) defines the store layout, and the
writers existed for the reference store. What was missing:
- a converter from real SpatialData;
- serving converted SpatialData tables;
- the browser view.

Example data: two MERSCOPE regions, each with:
- 3 single-chunk images (4296 × 4890, 3 channels);
- 2 masks;
- 6,125 cell outlines and 81,787 tile outlines;
- 5.6 M transcripts of 500 genes;
- two tables.

## Decisions

### Converter (`dbx.convert.spatialdata`)
- **Tables** go through the AnnData path. `write_table` is factored out of `convert_anndata`, so
  X becomes gene-major CSC with gene statistics, as for h5ad.
  - Each table keeps `region`, `region_key` and `instance_key`.
  - A chosen layer missing from a table falls back to X, with a warning.
- **Images** are read lazily (dask) and written tile by tile: level 0 as it is, each next level
  (2×2 mean) from the one just written. An image larger than memory is fine.
  - Channel windows are the 0.5–99.5 percentiles of the smallest level, also for 3-channel uint8
    images. The examples' "RGB" images are dim DAPI stains, not photographs.
- **Labels:**
  - relabelled when IDs are sparse or 64-bit;
  - a row lookup when a table annotates them;
  - read whole (see limits).
- **Shapes:**
  - polygons and multipolygons are split into outer rings sharing a row (holes are dropped, as
    decided in v0.1);
  - circles where the source has points with a radius;
  - the row is the annotating table's row of the shape's instance id, else the shape's position.
- **Points:**
  - The feature column is the declared `feature_key`; else the only categorical column left once
    instance-id columns are excluded; else the categorical column named like genes.
  - Ambiguity fails the job, with `feature_key_ambiguous`.
  - `cell` links a transcript to the row of the table whose instance key the points carry. For
    MERSCOPE, `cell_id` links to `table_cells`.
- **Transforms:** one 3×3 affine per element and coordinate system, from `spatialdata`'s
  `to_affine_matrix`. Units are `unknown` until SpatialData declares them, so there is no scale
  bar yet.
- Every chunk is written, also all-zero ones. A missing chunk is a 404 in the browser, and
  console noise.
- As for AnnData: validated deeply, published atomically, memory guard and disk checks.
  - The memory estimate counts tables, points (48 B each), shapes and labels.
  - Progress follows the steps tables → images → labels → shapes → points.
- **Measured** (M2):

  | Dataset | Time | Peak memory |
  |---|---|---|
  | MERSCOPE region (5.6 M transcripts) | 10 s | 1.7 GB |
  | Synthetic: 200 k cells, 5 M transcripts, 8 k × 8 k × 2 uint16 image | 12 s | 1.5 GB |

  Outlines take 6 s per 200 k cells (a Python loop per ring).

### Serving
- `store_table(cache, fp, table)` finds a converted table by its source name. Meta, genes and
  subsets of SpatialData tables come from the store, as for h5ad.

### Spatial view (`views/spatial`)
- Offered for converted SpatialData datasets ("Open spatial view"; "Open embedding" stays).
- **Image:** Viv `MultiscaleImageLayer` on the store's zoom levels, with per-channel on/off,
  colour and contrast. The first three channels with signal start on.
- **Cells:**
  - the outline element annotated by the view's table;
  - filled or outlined;
  - coloured on the GPU by the shared colouring (`views/colouring.tsx`, moved out of the
    embedding view): one value per vertex, from the polygon's table row;
  - tooltips name the cell's category or value.
- **Large cell sets** (more than `MAX_VERTICES` = 4 M vertices):
  - cell centres when zoomed out;
  - the outline blocks of 4,096 cells in view once at most `OUTLINE_BUDGET` = 300 k cells are
    in view (`block_bbox`, then slices of `starts`, `vertices`, `row`).
- **Transcripts** of up to 6 genes, one colour each:
  - a gene's range of the gene-sorted store;
  - above the store's cap (100 k), the chunks of its range in view when about 1.5 M fit, else
    the even sample (`lod/`).
- **Placement:** each element by its affine for the chosen coordinate system (model matrix). The
  view's y grows downwards.
- **Canvas changes:**
  - each view publishes the area it shows (snapped to a 256 px grid at half zoom steps), so
    views can read what is in view without re-reading on every frame;
  - layers are filtered by their top-level layer (Viv's sub-layers have their own ids);
  - tooltips receive the layer id.
- **Section grid (F11c):** the embedding's "Split by" (ADR 0010), e.g. `spatial` coordinates by
  embryo or section.

## Known limits
- **Read whole, and estimated by the memory guard:** masks, outlines and transcripts. That is
  fine for the host's memory and the data seen so far; streaming is for very large panels.
- **Not yet:**
  - the z-plane filter for transcripts (no example has z);
  - colouring masks (labels) by cell (plan: stretch);
  - a scale bar (units unknown);
  - 3-D images.
- The outline conversion loops in Python per ring: about 30 s per 1 M cells (vectorisable with
  shapely 2 if needed).

## Evidence
- `tests/backend/test_convert_spatial.py`:
  - synthetic golden checks (image level 0 equal, label lookup, transcripts per gene,
    transforms);
  - the feature-column rule;
  - the job, and serving the converted table;
  - the MERSCOPE example (counts, inferred `gene_id`, the cell link).
- e2e:
  - convert region_1, open the spatial view: coloured cells, a tooltip, the image, transcripts of
    one gene;
  - with lowered limits, cell centres zoomed out and outline blocks after zooming in.
