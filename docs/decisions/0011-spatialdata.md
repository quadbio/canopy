# 0011 SpatialData conversion and the spatial view (S6)

Date: 2026-10-11 · Status: accepted (S6; revised 2026-10-12 after review S6, batch R6)

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
- **Reading:** `spatialdata.read_zarr` reads images, labels, shapes and points only. Tables go
  through `write_table` (the AnnData path), so no table matrix is ever loaded whole by the
  reader (review S6, A-B2: it had loaded every matrix, uncounted).
- **Tables:**
  - X becomes gene-major CSC with gene statistics, as for h5ad. Each table keeps `region`,
    `region_key` and `instance_key`; a chosen layer missing from a table falls back to X, with
    a warning.
  - A table over several regions (the usual multi-section layout) is supported (A-B1); its
    region column becomes the table's `section_keys` (SPEC §4.6), for one panel per section.
  - Regions naming elements the store lacks are dropped with a warning.
- **Images** are read lazily (dask) and written block by block: level 0 as it is, each next
  level (2×2 mean) from the one just written. A chunked image larger than memory is fine; an
  image stored as one chunk is read whole (counted in the estimate).
  - Levels larger than 8192 px are sharded (1 × 4096 × 4096): far fewer files on network
    storage. Each write task writes whole shards.
  - Channel windows are the 0.5–99.5 percentiles of the first level at most 2048 px wide (the
    smallest level averaged sparse signal away).
  - `rgb` only for uint8 images whose channels are named r, g, b(, a).
  - Dask uses `jobs.threads` threads (default 4), not one per core of the shared host.
- **Labels** are written block by block too (A-M5): the IDs are collected per block, then each
  block is relabelled (sparse or ≥ 2³² IDs; uint64 IDs ≥ 2⁶³ kept) or cast to uint32 as level 0
  is written. The label → table-row lookup is built from the table's instance ids.
- **Shapes** (vectorised with shapely 2, A-M2):
  - polygons and multipolygons are split into outer rings sharing a row (holes are dropped, as
    decided in v0.1);
  - invalid polygons are repaired (`make_valid`) and keep their polygon parts; rings without
    area or with non-finite vertices are dropped; rows that are not polygons are left out (not
    the element). Each case is a counted warning;
  - circles where the source has points with a radius;
  - the row is the annotating table's row of the shape's instance id (within the shape's own
    region), else the shape's position.
- **Points:**
  - Read one partition at a time, only the columns the store needs (x, y, z, gene, cell), into
    compact arrays (float32 positions, int32 gene codes and table rows; A-M1). Sorting uses one
    uint64 key per point (gene, Morton code), with numpy's default (unstable, deterministic)
    sort: equal keys are the same gene in the same Morton cell.
  - Points with non-finite x/y, NaN z or no gene are dropped (counted, A-M3).
  - The feature column: the conversion's `feature_key` option, else the declared one, else the
    only categorical column left once instance-id columns are excluded, else the one
    (categorical, then text) named like genes. Ambiguity fails the job with
    `feature_key_ambiguous`; the conversion widget then asks for the column.
  - `cell` links a transcript to its table row. Only the regions of the linked table that share
    a coordinate system with the points count; if instance ids still repeat across them, there is
    no link and a warning, never a guess (A-M4). The linked fraction is logged.
- **Transforms:** one 3×3 affine per element and coordinate system, from `spatialdata`'s
  `to_affine_matrix`. Units come from the elements' NGFF axis units where declared (micrometer,
  pixel), else `unknown`.
- Every chunk is written, also all-zero ones. A missing chunk is a 404 in the browser.
- The manifest is strict JSON (no NaN); the validator rejects non-finite numbers.
- As for AnnData: validated deeply, published atomically, a disk check before starting
  (A-m1), the memory guard, `converter.peak_rss_gb` in the manifest.
- **Progress and cancel** (A-M6): progress is reported before each element and inside it
  (dask callbacks for images and masks; per partition read, after the sort steps and per array
  written for transcripts), so progress lines name the stage that is running and a cancel acts
  at the next report: measured gaps are under 1 s for 10 M transcripts, a 12 k² mask and a
  single-chunk image. The final deep check is one step (about 0.16 s per million transcripts).
- **Logs:** element sizes (shape, label count, relabelled, vertices, dropped and repaired
  counts, column dtypes, linked fractions), element counts in `job_done`; a failed validation
  logs `problems` as element (tokenised at strict) plus message with names replaced (A-m2).
- **Memory estimate** (`spatial_memory_estimate`), calibrated by `bench/spatial_memory.py`
  (bench/README.md): tables' obs/var, shapes (2.6 KB each), the largest of: points (100 B each
  plus one partition read at 5× the bytes of the columns read, with the cell column the
  converter links), image and mask blocks in flight (5 × block × threads, or the whole
  single-chunk source); plus 256 MB. Measured peaks stay below it (ratio 1.2–2.7; the MERSCOPE
  regions 1.2, needing about 3.1 GB free in the demo configuration). A job needing
  more than the host's total memory fails (`not_enough_memory`) instead of waiting forever, and
  a job waiting for memory no longer holds back smaller ones (A-m7, B-m14).
- **Measured** (M2):

  | Dataset | Time | Peak memory |
  |---|---|---|
  | MERSCOPE region (5.6 M transcripts) | 10 s | 1.7 GB |
  | Synthetic: 200 k cells, 5 M transcripts, 8 k × 8 k × 2 uint16 image | 12 s | 1.5 GB |
  | Synthetic elements (bench/spatial_memory.py, scale 1) | 1.4–8.7 s each | 0.7–2.3 GB |

### Serving
- `store_table(cache, fp, table)` finds a converted table by its source name. Meta, genes and
  subsets of SpatialData tables come from the store, as for h5ad.
- Static store files: whole files are cacheable forever (`immutable`, ETag, 304). Range replies
  are `no-store` and never 304 (B-B2: the browser's cache, combining parts of shard files under
  many concurrent ranges, returned empty bodies).
- Store requests are logged in aggregate, once a minute per store and element kind
  (`store_requests`: count, bytes, statuses, mean and max time); failures and slow replies one by
  one (B-M6; one line per range had filled 13.5 MB in 20 minutes).

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
    in view (`block_bbox`, then slices of `starts`, `vertices`, `row`). While blocks load,
    centres are drawn only where outlines are not drawn yet.
- **Transcripts** of up to 6 genes, one colour each (kept while the gene is shown):
  - a gene's range of the gene-sorted store;
  - above the store's cap (100 k), its chunks in view when they hold at most 1.5 M transcripts
    (counted from `chunk_bbox`, not assumed even), else the even sample (`lod/`).
- **Reading** (`api/store.ts`, B-B2, B-M4): no browser HTTP cache; opened arrays (metadata,
  shard indexes) cached per store; decoded chunks cached (256 MB, least recently used out), so
  panning reads only new chunks; whole small arrays read with one request per shard file; every
  read takes an abort signal (stale reads stop); a failed or short read is retried once, then
  shown ("n of m outline blocks did not load", Retry) and counted (`store_reads`,
  `spatial_blocks_failed`). Images go through the same store (no zarr v2 probes, no 404s).
- **Drawing** (B-B1, B-M2): one deck.gl data object per polygon source and colour values, so
  other settings never re-triangulate polygons; filled and outlined cells are different layer
  types with different ids. Image contrast sliders span a range derived from the converter's
  window (with "auto" to go back), not the whole data type (B-M7). A single channel shown
  alone starts grey scale.
- **Placement:** each element by its affine for the chosen coordinate system (model matrix). The
  view's y grows downwards.
- **Telemetry** (B-M6): `view_ready` for the spatial view; `deck_error` with the layer class
  and a name-free message (once per kind and 30 s, with a repeat count); `frames` (median, 95th
  percentile, long frames per 10 s of drawing); `panels` when a view's panel count changes.
- **Canvas changes:**
  - each view publishes the area it shows (snapped to a 256 px grid at half zoom steps), so
    views can read what is in view without re-reading on every frame;
  - layers are filtered by their top-level layer (Viv's sub-layers have their own ids);
  - tooltips receive the layer id.
- **Section grid (F11c):** the embedding's "Split by" (ADR 0010), e.g. `spatial` coordinates by
  embryo or section.

## Known limits
- **Read whole, and estimated by the memory guard:** shapes (geopandas, about 2.6 KB per cell:
  Visium HD 2 µm bins, 11 M squares, need about 28 GB) and transcripts (100 B each: 10⁹ need
  about 100 GB). Streaming both is the next step for such data (partition by gene, sort each);
  the guard refuses what the host cannot hold rather than letting it run out of memory.
- An image or mask stored as one chunk is read whole.
- **Not yet:**
  - the z-plane filter for transcripts (no example has z);
  - colouring masks (labels) by cell (plan: stretch);
  - a scale bar (units are now read where declared; the examples declare none);
  - 3-D images (skipped with a warning naming the element);
  - a section grid of the spatial view itself (one panel per region with its image and cells):
    the embedding's "Split by" serves sections of spatial coordinates; `section_keys` is now
    written for it.
- Viv's image layer logs one harmless luma.gl warning (`instancePickingColors`) per view.

## Evidence
- `tests/backend/test_convert_spatial.py`:
  - synthetic golden checks (image level 0 equal, label lookup, transcripts per gene,
    transforms);
  - a table over two regions: section key, shapes rows per region, the cell link per
    coordinate system, and no link when ids repeat in one system;
  - messy geometries (self-intersecting, flat, clockwise, multipolygon, line) and non-finite
    transcripts: converts, valid, strict-JSON manifest;
  - sparse and uint64 label IDs ≥ 2⁶³: relabelled, lookup, nearest pyramid;
  - an image wider than 8192 px: sharded level 0, exact values;
  - the feature-column rule (option, text columns) and store names (no collisions, no hidden
    names);
  - the job, and serving the converted table;
  - the MERSCOPE example: counts, inferred `gene_id`, and per gene the same cell rows as the
    source.
- `tests/backend/test_convert.py`: a job larger than the host fails, a waiting job does not block
  a smaller one. `test_datasets.py`: ranges never 304, `no-store`. `test_contract.py`:
  non-finite manifests fail; validator problems without names.
- `bench/spatial_memory.py`: estimate against measured peak per element kind.
- e2e:
  - convert region_1, open the spatial view: coloured cells, a tooltip, the image, transcripts of
    one gene, no console errors or 404s;
  - Filled → Outlines → Filled → Outlines keeps the cells coloured; cell opacity sends no polygon
    data again;
  - with lowered limits: cell centres zoomed out, outline blocks arriving after one zoom step
    with no failures, block outlines in each cell's colour, panning back without failed reads.
