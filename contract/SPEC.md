# Canopy data contract, version 0.2 (approved 2026-10-10)

The data contract defines **Canopy stores**: the converted form of a dataset that the Canopy server and
browser read at full speed. It also states what the **direct preview** may read from original
files. The machine-readable part is `schema/manifest.schema.json` (generated from
`backend/canopy/contract/models.py`); `canopy validate <store>` checks a store against both.

Status: **v0.2 approved** (2026-10-10, option A), replacing v0.1 before any converter wrote
stores. The v0.1 review decisions (§11) are unchanged. What changed and why:
`history/v0.1-to-v0.2-comparison.md`; every change is marked **[v0.2]** below.

---

## 1. Principles

1. **A strict profile of standard formats.** Tables are AnnData-zarr; images and labels are
   OME-NGFF 0.5, so stock tools (anndata, Vitessce, napari-ome-zarr) are intended to open those
   parts directly (tested: anndata, Viv and zarrita; Vitessce and napari not yet checked).
   Only the parts with no standard (cell polygons, transcript partitions, lookups, statistics)
   use plain zarr arrays defined here.
2. **Values are never changed, only re-laid-out.** Expression values, coordinates, categories,
   colours and transforms are copied exactly (float32 where the source is float64, see §4.2).
   Golden tests compare every store with its source.
3. **Everything the browser needs is precomputed or declared**, so the browser never parses raw
   SpatialData, never guesses which column holds genes, and never composes transform chains.
4. **Stores are rebuildable caches.** They live in the cache area, never next to source files;
   deleting one only costs a re-conversion.
5. **zarr v3 throughout** (anndata 0.13 default; zarrita 0.5 and Viv 0.23 read it).
6. **Arrays the browser reads directly are numeric.** zarrita 0.5.4 (the version Viv 0.23 uses)
   cannot decode zarr v3 variable-length strings, so names (genes, channels) needed by the browser
   are stored as JSON attributes. Strings inside AnnData tables are fine: the server reads those.
   int64 arrays arrive in JavaScript as `BigInt64Array`, which deck.gl cannot use directly, so
   **[v0.2]** every offset or index array the browser reads is uint32/int32 while the counts allow
   (below 2³² or 2³¹), and int64 only beyond that. The manifest states each such dtype.
7. **[v0.2] Everything the browser reads is chunked for partial reads over HTTP.** Large arrays
   are split into chunks of roughly 50–500 KB compressed and grouped into shard files (zarr v3
   sharding), so the store has few files while the browser fetches only the chunks it needs
   with HTTP range requests. The server must answer `Range` requests, including suffix ranges
   (`bytes=-N`, used to read shard indexes), and clamp ranges that run past the end of a file.

## 2. Store location and layout

```
<cache_dir>/<dataset_id>/c<contract_major>/        e.g. cache/7a88e360c210c450/c0/
  manifest.json                    the manifest (§3); written last, atomically
  zarr.json                        root zarr group (attributes: {"canopy": {"contract_version": …}})
  tables/<table>/                  AnnData-zarr (§4)
  tables/<table>/uns/canopy/          per-table extras: gene statistics, downsampling (§4.4–4.5)
  images/<image>/                  OME-NGFF 0.5 multiscale image (§5)
  labels/<labels>/                 OME-NGFF 0.5 multiscale labels (§6)
  aux/labels/<labels>/             label → table-row lookup (§6)
  shapes/<shapes>/                 polygons or circles as flat arrays (§7)
  points/<points>/                 transcripts partitioned by feature (§8)
```

- `dataset_id` comes from the source fingerprint: a hash of the dataset's place (its data root's
  name and its path inside the root; the real path for a file outside any root) plus, for a file,
  its size and modification time, or, for a zarr store, the newest modification time of its groups
  and metadata files. A changed source gets a new store. (Rewriting chunks of a zarr store in place
  without touching any metadata is not detected; such stores need a manual rebuild.)
- A store is built in `<…>/c0.tmp-<random>/` and renamed to `c0/` only after `canopy validate`
  passes. A folder without `manifest.json` is incomplete and is ignored.
- **Consolidated metadata** is written for each table group (anndata reads through it) and for
  the store root, *after* all arrays are written. Stale consolidated metadata hides arrays from
  anndata and is a validation error.
- Element names are the source's names (SpatialData element names; `table` for a plain AnnData
  file). Names are restricted to `[A-Za-z0-9._-]`; other characters are replaced by `_` and the
  original name is kept in the manifest (`source_name`). **[v0.2]** Each element's `path` is
  exactly `<kind>/<name>`; the validator checks both rules.

## 3. Manifest

`manifest.json` lists everything in the store. It is strict JSON: no NaN or Infinity (a
validator error), since browsers cannot parse them. The browser reads only the manifest to know what
exists, which views are possible and how to place elements. Fields (full schema in
`schema/manifest.schema.json`):

| Field | Content |
|---|---|
| `contract`, `contract_version` | `"canopy-store"`, `"0.1.0"` |
| `dataset_id` | from the source fingerprint |
| `source` | path, kind (`h5ad` / `anndata_zarr` / `spatialdata_zarr`), size, mtime, fingerprint version |
| `converter` | canopy version, created (UTC), duration, peak memory, warnings |
| `coordinate_systems` | names of all coordinate systems (`global` first if present) |
| `units` **[v0.2]** | coordinate system → unit of its `x, y`: `"micrometer"`, `"pixel"` or `"unknown"` (from SpatialData axis units; for h5ad `spatial` coordinates, a converter option, default `"unknown"`). The browser draws a scale bar only for physical units |
| `tables` | per table: §4 fields |
| `images`, `labels`, `shapes`, `points` | per element: §5–§8 fields |

Every spatial element carries **`transforms`: a map from coordinate-system name to a 3×3 affine
matrix** (row-major, homogeneous 2-D, mapping the element's *intrinsic* `x, y` to that system's
`x, y`). The converter composes SpatialData transformation chains (sequence, scale, translation,
affine, identity) into this single matrix with `spatialdata`; the browser applies it as a
deck.gl model matrix. Elements have no transform to systems they are not placed in.

## 4. Tables (AnnData-zarr profile)

Written with `anndata` (`write_zarr`, zarr v3). Requirements beyond plain AnnData:

### 4.1 X: gene-major CSC

| Item | Rule |
|---|---|
| Encoding | `csc_matrix` (also for sources that were dense or CSR) |
| `data` | float32 |
| `indices` | int32 (cell index); int64 only if `n_obs ≥ 2³¹` |
| `indptr` | int64 |
| Chunks | `data`, `indices`: 262,144 values (1 MiB float32); `indptr`: 65,536 |
| Sharding | allowed for `data`/`indices` (shards of 64 chunks) when the store would exceed 100,000 files |
| Codec | zstd, level 3 |

A gene query reads `indptr[j:j+2]`, then one contiguous range of `data`/`indices`.

### 4.2 Values

- X values are copied as float32. Integer counts above 2²⁴ (16,777,216) cannot be represented
  exactly and produce a converter warning.
- Which matrix becomes X: **the source X, always by default**. Optionally the person converting
  may name a source layer instead (e.g. `counts`); this choice is never required. The manifest
  records `x_source` (`"X"` or `"layers/<name>"`). Exactly one matrix is copied; other layers are
  not copied.
- `raw` is not copied in v0. If the source X is missing but `raw/X` exists, `raw/X` becomes X and
  `x_source` is `"raw/X"`.
- `obsp` (neighbour graphs), `varm` and `varp` are not copied (not needed for viewing).

### 4.3 obs, var, obsm, uns

- `obs` and `var`: copied with anndata's encodings (categoricals, nullable types, strings).
- `obsm`: **every numeric 2-D array with at least 2 columns is copied** as float32 (UMAP, t-SNE,
  PCA, diffusion maps, … and spatial coordinates; `spatial` or keys starting with `spatial` are
  listed as `spatial_keys`). Chunks: all columns, 65,536 rows. Entries that are data frames or
  have one column are skipped with a converter warning. The browser lets the user switch between
  embeddings and, for an embedding with more than two dimensions, choose which two dimensions
  are plotted (default: the first two).
- `uns`: only `<column>_colors` entries are copied, validated as CSS colour strings
  (`#rrggbb`, `#rrggbbaa` or a named colour converted to hex). A colour list whose length does not
  match its column's categories is dropped with a warning.

### 4.4 Gene statistics (`uns/canopy/gene_stats`)

One float32 array per statistic, length `n_vars`: `nnz` (stored as float32 count), `mean`,
`max`, `p99` (99th percentile of non-zero values). Used for colour scaling and to choose the
quantisation range of gene values sent to the browser.

### 4.5 Downsampling (`uns/canopy/downsample`)

For tables with more than 200,000 cells: `uns/canopy/downsample/<n>` holds sorted cell
indices (int32; int64 only if `n_obs ≥ 2³¹`) for `n` = 200,000 and 1,000,000 (when smaller than `n_obs`). Sampling is stratified by
the first categorical column with colours (else uniform), with a fixed seed, so every category
stays visible.

### 4.6 Manifest fields per table

`path`, `n_obs`, `n_vars`, `x_source`, `x_dtype`, `embeddings` (key → number of dimensions), `spatial_keys`,
`categorical` (column → number of categories, has colours), `numeric` (column names),
`var_names_column` (column holding display gene names if not the index, e.g. `gene_symbols`),
`downsample` (sizes), `region` / `region_key` / `instance_key` (SpatialData tables: which element
rows the table annotates), **[v0.2]** `section_keys`: obs columns (categorical) that identify
sections or samples, coarse to fine (e.g. `["embryo", "position", "z"]`). They come from a
converter option; a SpatialData table that annotates several regions gets its `region_key`
automatically. The browser offers them for faceting (one panel per section) and filtering.

## 5. Images (OME-NGFF 0.5)

| Item | Rule |
|---|---|
| Axes | `c, y, x` (2-D) — `c, z, y, x` is reserved for a later version |
| Pyramid | level 0 = source resolution; each next level halves `y`, `x` (mean for intensities) until both are ≤ 512 |
| Chunks | `1 × 512 × 512` (one channel per chunk) |
| Sharding | levels larger than 8192 px (either side) are sharded: `1 × 4096 × 4096` (a side under 512 px: its full length); smaller levels are not. Writers write whole shards per task |
| dtype | unchanged from the source (uint8, uint16, float32) |
| Codec | zstd, level 3 |
| Metadata | NGFF `multiscales` with scale transforms per level; `omero.channels[]` with `label` and `window` (`start`/`end` = 0.5th / 99.5th percentile from the first level at most 2048 px wide; `min`/`max` = dtype range) |

RGB(A) images are stored as 3 or 4 channels and flagged `rgb: true` in the manifest, so the
browser can show them in true colour by default. Only uint8 images whose channels are named
r, g, b(, a) (or red, green, blue(, alpha)) are flagged: three uint8 stains are not a photograph.

Manifest fields: `path`, `axes`, `dtype`, `levels` (shapes), `channels` (name, window, colour
hint), `rgb`, `transforms`.

## 6. Labels (segmentation masks)

| Item | Rule |
|---|---|
| Format | OME-NGFF 0.5 multiscale labels, axes `y, x`, with NGFF `image-label` metadata **[v0.2]**; the `labels/` group lists its members in `ome.labels`, so NGFF tools recognise them as labels |
| Pyramid | as images, but **nearest-neighbour** downsampling (every second pixel), never averaging |
| dtype | uint32 (int32 sources with non-negative values are re-typed). **[v0.2]** Negative values are treated as background (0), with a converter warning |
| Relabelling **[v0.2]** | IDs are kept when `max_label < 2³²` and `max_label ≤ 4 × n_labels + 1024`. Otherwise (64-bit or sparse IDs, e.g. entity IDs) the converter **relabels** to 1…N in order of the original IDs, stores the original IDs in `aux/labels/<name>/original_id` (uint64, length N + 1, entry 0 = 0) and sets `relabelled: true`. Lookups and the table link use the new IDs |
| Chunks / codec | `512 × 512`, zstd level 3 |
| Lookup | `aux/labels/<name>/row`: int32 array of length `max_label + 1`; entry `v` is the table row annotating label value `v`, or −1. Only written when a table annotates this labels element |

The browser colours a label pixel by looking up its row and then the row's colour (category or
gene value), on the GPU.

Manifest fields: `path`, `dtype`, `levels`, `max_label`, `n_labels` **[v0.2]**, `relabelled`
**[v0.2]**, `table` (annotating table or null), `lookup` (path or null), `transforms`.

## 7. Shapes

Two kinds, chosen from the source geometry:

**Polygons** (`kind: "polygons"`; cell boundaries; MultiPolygons are split into parts sharing a row):

| Array | dtype | Shape | Meaning |
|---|---|---|---|
| `vertices` | float32 | (V, 2) | `x, y` of each polygon's outer ring in intrinsic coordinates (holes dropped). **[v0.2]** Rings are **closed** (last vertex repeats the first) and wound **counter-clockwise** (positive shoelace area in `x, y` as stored), so deck.gl can use them without its per-polygon clean-up pass |
| `starts` | **[v0.2]** uint32 (uint64 if V ≥ 2³²) | (P + 1,) | polygon `p` uses `vertices[starts[p]:starts[p+1]]` |
| `row` | int32 | (P,) | table row of polygon `p`, or the shape's index position when no table annotates it |
| `bbox` | float32 | (P, 4) | `xmin, ymin, xmax, ymax` |
| `centroid` **[v0.2]** | float32 | (P, 2) | for the zoomed-out view, where cells are drawn as dots instead of outlines |
| `block_bbox` **[v0.2]** | float32 | (B, 4) | bounding box of polygon block `b` = polygons `[b·4096, (b+1)·4096)` |

**[v0.2] Spatial order.** Polygons are sorted by the Morton code of their centroid (area centroid
of the closed ring, quantised to 16 bits per axis within the bounding box of all centroids),
so each block of 4,096 polygons covers a compact area. When zoomed in, the browser reads
`block_bbox`, then only the blocks in view: their `starts` slice, then one contiguous range of
`vertices`. When zoomed out it draws `centroid` (8 bytes per cell) instead of outlines. `row` keeps
the link to the table, so table order is unaffected.

**Circles** (`kind: "circles"`; e.g. Visium spots): `center` float32 (N, 2), `radius` float32 (N,),
`row` int32 (N,).

Messy sources: invalid polygons (self-intersecting, …) are repaired (`make_valid`) and keep
their polygon parts; rings without area or with non-finite vertices are dropped; rows that are
not polygons (lines, points among polygons) are left out, not the element. Each case is a
counted warning (`shapes_invalid_repaired`, `shapes_degenerate_dropped`,
`shapes_non_polygon_dropped`).

Chunks **[v0.2]**: `vertices` 65,536 rows; per-polygon arrays 4,096 rows (one block); shards of 64
chunks; codec zstd. Manifest fields: `path`, `kind`, `n`, `n_vertices` **[v0.2]**, `table`,
`transforms`.

## 8. Points (transcripts)

Full-resolution arrays (all points, sorted as below):

| Array | dtype | Shape | Meaning |
|---|---|---|---|
| `xy` | float32 | (N, 2) | intrinsic coordinates |
| `z` | float32 | (N,) | z coordinate or z-plane index, only when the source has one |
| `cell` | int32 | (N,) | table row of the assigned cell, −1 if none (only when the source has an instance column) |
| `offsets` | uint32 (uint64 if N ≥ 2³²) **[v0.2]** | (F + 1,) | points of feature `f` are `[offsets[f], offsets[f+1])` |
| `chunk_bbox` **[v0.2]** | float32 | (C, 4) | bounding box of each 16,384-row chunk of `xy` |
| *(attribute)* `canopy.features` | list of strings | F | feature names (genes), in the points group's `zarr.json` attributes |

**[v0.2]** The v0.1 `feature` array (one gene index per point) is dropped: in a gene-sorted layout
it repeats what `offsets` already says.

**Reduced views [v0.2]**, each a small group with the same arrays (`xy`, `z`, `cell` when present),
sorted the same way:

| Group | Content | Used for |
|---|---|---|
| `overview/` | a uniform random sample of up to 1,000,000 points across all genes, plus `feature` (int32) and `offsets` | the zoomed-out "all transcripts" view: one contiguous download (≈ 7 MB) instead of every chunk |
| `lod/` | for each gene with more than 100,000 points, a spatially even sample of 100,000 of them (other genes: empty range), plus `offsets` | a highly expressed gene seen zoomed out: at most 100,000 points per gene, whatever the dataset size |

- Points are **sorted by feature, then by Morton (Z-order) code** of `x, y` quantised to 16 bits
  per axis within the element's bounding box. One gene's transcripts are one contiguous range,
  and spatially close points are close in storage.
- **How the browser reads one gene [v0.2]:** zoomed out, from `lod/` when the gene has more than
  100,000 points, else its full range; zoomed in, only the chunks of its range whose
  `chunk_bbox` intersects the view.
- **Chunks [v0.2]:** 16,384 rows (≈ 110 KB compressed for `xy`), grouped in shards of 64 chunks
  (one file per ~1 M points). Measured on the MERSCOPE example (5.6 M transcripts): one median gene
  downloads 0.11 MB instead of 6.9 MB, and the zoomed-out view 6.7 MB instead of 39 MB
  (`bench/contract_v02_points.py`).
- The **feature column** is the column chosen for the conversion (option `feature_key`), else
  the source's declared `feature_key`; if none is declared, the converter picks the single
  categorical/dictionary string column, else the one (categorical, then plain text) named like
  genes, and records `feature_key_inferred: true`; if that is ambiguous, the job fails with
  `feature_key_ambiguous` and the user names the column.
- Points with a non-finite `x`/`y` (or NaN `z`) or without a gene are dropped (counted
  warnings). The cell link uses only the regions of the linked table that share a coordinate
  system with the points; when instance ids still repeat across those regions, there is no link
  (`points_cell_link_ambiguous`), never a guess.
- Manifest fields: `path`, `n`, `n_features`, `feature_key`, `feature_key_inferred`, `has_cell`,
  `has_z`, `z_range` (min, max), `z_planes` (sorted distinct z values when there are at most 256,
  e.g. MERSCOPE z-stacks; otherwise null), `bbox`, `n_overview` **[v0.2]**, `lod_cap` **[v0.2]**
  (100,000), `table`, `transforms`.
- **Display is 2-D.** `z` is never used for placement; it drives a **z filter** in the browser:
  the user picks one or more z-planes (when `z_planes` is set) or a z range, and only points
  inside it are drawn (GPU filter on the loaded points; no re-read from the server).

## 9. Versions and compatibility

- `contract_version` is semantic: **patch** = clarifications, no store change; **minor** = new
  optional fields or elements; **major** = layout change, stores must be rebuilt. The store
  folder is named by the major version (`c0/`).
- **[v0.2] Readers** (server and browser) accept any store of their own major version:
  - unknown manifest fields and unknown elements are **ignored**, so a store written by a newer
    minor version still opens, minus the new features;
  - a store whose minor version is newer than the reader's is logged once as a warning
    ("store is newer than this server; upgrade for new features").
- **[v0.2] The validator** (`canopy validate`) is strict for stores of its own version: an unknown
  field is an error unless the store's minor version is newer than the validator's (then it is a
  warning). A store of an older major version is an error ("rebuild needed").
- The server rebuilds a store automatically when its major version is older than the server's.
- Every change is recorded in `contract/CHANGELOG.md` and a decision record.

## 10. Direct preview (no store yet)

What the server may read straight from an original file, without conversion:

| Source | Served directly | Not served until converted |
|---|---|---|
| h5ad / AnnData-zarr **≤ `small_file_gb`** (default 2 GB) | everything (file loaded into memory) | — |
| h5ad / AnnData-zarr, larger, **X is CSC** | obs, var, embeddings, colours; gene queries (column read via `indptr`) | gene statistics, downsampling |
| h5ad / AnnData-zarr, larger, **X is CSR or dense** | obs, var, embeddings, colours | **gene queries** (would read all of X) |
| SpatialData | tables as above; images/labels only if already multiscale with chunks ≤ 1024 px and no transform chain the browser cannot apply | points, shapes, single-chunk or single-level images |

The catalog shows which of these apply to each file (from the fingerprint warnings).

## 11. Review decisions on v0.1 (2026-10-10, unchanged in v0.2)

1. **X** is the source X by default. Choosing a layer instead is an optional converter setting,
   never mandatory (§4.2).
2. **One matrix only**: other layers, `raw` and neighbour graphs (`obsp`) are not copied. **All
   embeddings are copied**; the browser switches between them and lets the user choose two
   dimensions for embeddings with more than two (§4.3).
3. **Spatial display is 2-D.** Point `z` is kept and powers a z-plane / z-range filter (§8).
   3-D images (`c, z, y, x`) remain reserved for a later version.
4. **Polygon holes** are dropped (confirmed).

## 12. What `canopy validate` checks [v0.2]

A store is only published (renamed from `c0.tmp-…` to `c0/`) when `canopy validate` passes. Beyond the
manifest schema, the validator checks every rule above that can be checked, including:

- **Layout:** chunk shapes, shard shapes and codec (zstd level) of every array; dtypes; element
  names and `path == "<kind>/<name>"`; consolidated metadata complete and current, for each table
  and for the root.
- **Tables:** X is CSC with sorted, unique row indices per gene (checked chunk by chunk);
  `indptr` runs 0…nnz; obs columns match the manifest's categorical / numeric lists;
  `var_names_column` and `section_keys` exist; `region` elements exist; colour list lengths.
- **Images / labels:** pyramid level shapes; channel windows within the dtype range; labels below
  2³²; lookup length `max_label + 1`; NGFF `image-label` metadata.
- **Shapes:** rings closed and counter-clockwise; `starts` non-decreasing, ending at V; block boxes
  contain their polygons; spatial (Morton) order of centroids.
- **Points:** sorted by feature, then Morton code within each feature; `offsets` 0…N; chunk boxes
  contain their points; `overview` and `lod` sizes, order and offsets; z within `z_range` /
  `z_planes`.
- **Memory:** `--deep` reads arrays **chunk by chunk** (bounded memory, also for 100+ GB stores);
  without `--deep`, only metadata and small arrays are read.
