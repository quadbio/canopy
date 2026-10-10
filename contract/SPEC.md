# dbx data contract, version 0.1 (approved 2026-10-10)

The data contract defines **dbx stores**: the converted form of a dataset that the dbx server and
browser read at full speed. It also states what the **direct preview** may read from original
files. The machine-readable part is `schema/manifest.schema.json` (generated from
`backend/dbx/contract/models.py`); `dbx validate <store>` checks a store against both.

Status: **v0.1 approved** (2026-10-10, with the review decisions in §11). Changes follow §9.

---

## 1. Principles

1. **A strict profile of standard formats.** Tables are AnnData-zarr; images and labels are
   OME-NGFF 0.5. Stock tools (anndata, Vitessce, napari-ome-zarr) can open those parts directly.
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
   int64 arrays arrive in JavaScript as `BigInt64Array`; they are used only for small offset
   arrays, and index arrays use int32 whenever the count is below 2³¹.

## 2. Store location and layout

```
<cache_dir>/<dataset_id>/c<contract_major>/        e.g. cache/7a88e360c210c450/c0/
  manifest.json                    the manifest (§3); written last, atomically
  zarr.json                        root zarr group (attributes: {"dbx": {"contract_version": …}})
  tables/<table>/                  AnnData-zarr (§4)
  tables/<table>/uns/dbx/          per-table extras: gene statistics, downsampling (§4.4–4.5)
  images/<image>/                  OME-NGFF 0.5 multiscale image (§5)
  labels/<labels>/                 OME-NGFF 0.5 multiscale labels (§6)
  aux/labels/<labels>/             label → table-row lookup (§6)
  shapes/<shapes>/                 polygons or circles as flat arrays (§7)
  points/<points>/                 transcripts partitioned by feature (§8)
```

- `dataset_id` comes from the source fingerprint (hash of real path and modification time), so a
  changed source file gets a new store.
- A store is built in `<…>/c0.tmp-<random>/` and renamed to `c0/` only after `dbx validate`
  passes. A folder without `manifest.json` is incomplete and is ignored.
- **Consolidated metadata** is written for each table group (anndata reads through it) and for
  the store root, *after* all arrays are written. Stale consolidated metadata hides arrays from
  anndata and is a validation error.
- Element names are the source's names (SpatialData element names; `table` for a plain AnnData
  file). Names are restricted to `[A-Za-z0-9._-]`; other characters are replaced by `_` and the
  original name is kept in the manifest (`source_name`).

## 3. Manifest

`manifest.json` lists everything in the store. The browser reads only the manifest to know what
exists, which views are possible and how to place elements. Fields (full schema in
`schema/manifest.schema.json`):

| Field | Content |
|---|---|
| `contract`, `contract_version` | `"dbx-store"`, `"0.1.0"` |
| `dataset_id` | from the source fingerprint |
| `source` | path, kind (`h5ad` / `anndata_zarr` / `spatialdata_zarr`), size, mtime, fingerprint version |
| `converter` | dbx version, created (UTC), duration, peak memory, warnings |
| `coordinate_systems` | names of all coordinate systems (`global` first if present) |
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

### 4.4 Gene statistics (`uns/dbx/gene_stats`)

One float32 array per statistic, length `n_vars`: `nnz` (stored as float32 count), `mean`,
`max`, `p99` (99th percentile of non-zero values). Used for colour scaling and to choose the
quantisation range of gene values sent to the browser.

### 4.5 Downsampling (`uns/dbx/downsample`)

For tables with more than 200,000 cells: `uns/dbx/downsample/<n>` holds sorted int32/int64 cell
indices for `n` = 200,000 and 1,000,000 (when smaller than `n_obs`). Sampling is stratified by
the first categorical column with colours (else uniform), with a fixed seed, so every category
stays visible.

### 4.6 Manifest fields per table

`path`, `n_obs`, `n_vars`, `x_source`, `x_dtype`, `embeddings` (key → number of dimensions), `spatial_keys`,
`categorical` (column → number of categories, has colours), `numeric` (column names),
`var_names_column` (column holding display gene names if not the index, e.g. `gene_symbols`),
`downsample` (sizes), `region` / `region_key` / `instance_key` (SpatialData tables: which element
rows the table annotates).

## 5. Images (OME-NGFF 0.5)

| Item | Rule |
|---|---|
| Axes | `c, y, x` (2-D) — `c, z, y, x` is reserved for a later version |
| Pyramid | level 0 = source resolution; each next level halves `y`, `x` (mean for intensities) until both are ≤ 512 |
| Chunks | `1 × 512 × 512` (one channel per chunk) |
| Sharding | allowed: shards of `1 × 4096 × 4096` for levels larger than 8192 px |
| dtype | unchanged from the source (uint8, uint16, float32) |
| Codec | zstd, level 3 |
| Metadata | NGFF `multiscales` with scale transforms per level; `omero.channels[]` with `label` and `window` (`start`/`end` = 0.5th / 99.5th percentile from the lowest level; `min`/`max` = dtype range) |

RGB(A) images are stored as 3 or 4 channels and flagged `rgb: true` in the manifest, so the
browser can show them in true colour by default.

Manifest fields: `path`, `axes`, `dtype`, `levels` (shapes), `channels` (name, window, colour
hint), `rgb`, `transforms`.

## 6. Labels (segmentation masks)

| Item | Rule |
|---|---|
| Format | OME-NGFF 0.5 multiscale labels, axes `y, x` |
| Pyramid | as images, but **nearest-neighbour** downsampling (every second pixel), never averaging |
| dtype | uint32 (int32 sources with non-negative values are re-typed) |
| Chunks / codec | `512 × 512`, zstd level 3 |
| Lookup | `aux/labels/<name>/row`: int32 array of length `max_label + 1`; entry `v` is the table row annotating label value `v`, or −1. Only written when a table annotates this labels element |

The browser colours a label pixel by looking up its row and then the row's colour (category or
gene value), on the GPU.

Manifest fields: `path`, `dtype`, `levels`, `max_label`, `table` (annotating table or null),
`lookup` (path or null), `transforms`.

## 7. Shapes

Two kinds, chosen from the source geometry:

**Polygons** (`kind: "polygons"`; cell boundaries; MultiPolygons are split into parts sharing a row):

| Array | dtype | Shape | Meaning |
|---|---|---|---|
| `vertices` | float32 | (V, 2) | `x, y` of all rings' outer boundaries in intrinsic coordinates (holes dropped in v0), ring not closed |
| `starts` | int64 | (P + 1,) | polygon `p` uses `vertices[starts[p]:starts[p+1]]` |
| `row` | int32 | (P,) | table row of polygon `p`, or the shape's index position when no table annotates it |
| `bbox` | float32 | (P, 4) | `xmin, ymin, xmax, ymax` |

**Circles** (`kind: "circles"`; e.g. Visium spots): `center` float32 (N, 2), `radius` float32 (N,),
`row` int32 (N,).

Chunks: 1,048,576 rows; codec zstd. Manifest fields: `path`, `kind`, `n`, `table`, `transforms`.
The browser renders these as binary deck.gl attributes directly.

## 8. Points (transcripts)

| Array | dtype | Shape | Meaning |
|---|---|---|---|
| `xy` | float32 | (N, 2) | intrinsic coordinates |
| `z` | float32 | (N,) | z coordinate or z-plane index, only when the source has one |
| `feature` | int32 | (N,) | index into `features` |
| `cell` | int32 | (N,) | table row of the assigned cell, −1 if none (only when the source has an instance column) |
| *(attribute)* `dbx.features` | list of strings | F | feature names (genes), in the points group's `zarr.json` attributes |
| `offsets` | int64 | (F + 1,) | points of feature `f` are `[offsets[f], offsets[f+1])` |
| `overview` | int32 (int64 if N ≥ 2³¹) | (M,) | sorted indices of a random sample of up to 1,000,000 points across all features, for the zoomed-out "all transcripts" view |

- Points are **sorted by feature, then by Morton (Z-order) code** of `x, y` quantised to 16 bits
  per axis within the element's bounding box. One gene's transcripts are one contiguous range,
  and spatially close points are close in storage.
- The **feature column** is the source's declared `feature_key`; if none is declared, the
  converter picks the single categorical/dictionary string column and records
  `feature_key_inferred: true`; if that is ambiguous, conversion asks (job fails with the
  candidates listed).
- Chunks: 1,048,576 rows; codec zstd. Manifest fields: `path`, `n`, `n_features`,
  `feature_key`, `feature_key_inferred`, `has_cell`, `has_z`, `z_range` (min, max),
  `z_planes` (sorted distinct z values when there are at most 256, e.g. MERSCOPE z-stacks;
  otherwise null), `bbox`, `table`, `transforms`.
- **Display is 2-D.** `z` is never used for placement; it drives a **z filter** in the browser:
  the user picks one or more z-planes (when `z_planes` is set) or a z range, and only points
  inside it are drawn (GPU filter on the loaded points; no re-read from the server).

## 9. Versions and compatibility

- `contract_version` is semantic: **patch** = clarifications, no store change; **minor** = new
  optional fields or elements (old readers ignore them); **major** = layout change, stores must be
  rebuilt. The store folder is named by the major version (`c0/`).
- The server rebuilds a store automatically when its major version is older than the server's,
  and serves stores of the same major with a lower or equal minor.
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

## 11. Review decisions (2026-10-10)

1. **X** is the source X by default. Choosing a layer instead is an optional converter setting,
   never mandatory (§4.2).
2. **One matrix only**: other layers, `raw` and neighbour graphs (`obsp`) are not copied. **All
   embeddings are copied**; the browser switches between them and lets the user choose two
   dimensions for embeddings with more than two (§4.3).
3. **Spatial display is 2-D.** Point `z` is kept and powers a z-plane / z-range filter (§8).
   3-D images (`c, z, y, x`) remain reserved for a later version.
4. **Polygon holes** are dropped (confirmed).
