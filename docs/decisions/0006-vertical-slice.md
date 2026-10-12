# 0006 Vertical slice: preview reads, binary API, frontend skeleton

Date: 2026-10-10 · Status: accepted (S4)

## Context
S4 proves the chain file → API → deck.gl on the example data and lays down the frontend
structure that all later views use (reviewer B's S4 checklist, review items S6–S8).

## Decisions

**Backend**
- Preview reads one element at a time (anndata's `read_elem` on h5py/zarr groups): an obs column,
  an embedding's two chosen columns, a colour list. Nothing is loaded whole, so the "small file
  loads fully" rule is not needed for the current layout. Legacy layouts (anndata < 0.8) are the
  exception: whole-file load if the fingerprint-based memory estimate is below
  `memory.small_file_gb` (review item S4), else 409 `needs_conversion:legacy_layout`.
- One LRU for open files and served arrays, bounded by `memory.cache_gb`, 64 open files and
  `memory.idle_minutes`; a source evicted during a request is reopened once.
- Arrays as little-endian binary with dtype/shape headers, ETag revalidation, gzip level 1 for
  `/api` (not for store files, which are read by range and already zstd-compressed).
- Categories beyond 10,000 (barcodes stored as categoricals) are not sent and not offered for
  colouring; codes are still served.
- Converted stores served at `/api/stores/<version>/c<major>/<key>` with single, suffix and
  clamped ranges, `immutable` caching, hidden keys answered 404 (Viv probes `.zattrs`).
  `check-store-js` now runs Viv + zarrita against a real `canopy serve`.
- Client-side telemetry goes to the server log (`client_event`), bounded to numbers and codes.

**Frontend**
- One deck.gl `Deck`, one `OrthographicView` per open view in a grid (≤ 9), `layerFilter` by
  view id prefix. Views are registered types (`views/registry.ts`); each view's hook runs in an
  invisible `ViewRunner` and publishes layers, bounds and a tooltip.
- Data via TanStack Query hooks only; workspace in zustand (JSON, persisted per browser).
- Colours computed in a Web Worker from codes/values (categorical palette from the dataset when
  valid, else a 30-colour palette led by Okabe–Ito; viridis/magma with a 1–99 % range).
- Radix primitives for controls; design tokens as CSS variables with light/dark.
- Default colouring: categorical columns named like cell type/cluster first, then columns with
  colours; legend in natural order with counts, show/hide per category, collapsible.

## Evidence
- Golden tests: API values equal anndata's for every obs column and embedding of both example
  h5ad files and both tables of a SpatialData example; tiny h5ad and zarr fixtures cover
  categorical with missing values, nullable integers, booleans, strings, dims selection.
- Playwright (software WebGL): browse, search, open `spatial.h5ad`, legend toggling, numeric
  colouring, two datasets in one canvas (one `<canvas>` element), no console errors.

## Changes after the S3–S4 code review (R1, 2026-10-10)
- Saved workspaces are validated on restore (unknown view types dropped, parameters normalised
  per view type, a notice when a saved column or embedding is gone); error boundaries per view and
  for the app, which offers "Reset workspace" instead of a blank page.
- Point size and opacity are uniforms; positions and colours are separate attributes with stable
  identities: a slider step uploads nothing, a colour change uploads only colours (e2e-tested by
  counting WebGL buffer bytes).
- Hidden categories are transparent and not reported on hover. They are still drawn into the
  picking buffer; a GPU-side mask comes with the shared colour shader (R2).
- Default palette: 54 colours from matplotlib's tab20/tab20b/tab20c without greys or black; the
  legend says when colours repeat. Opening or closing views no longer resets other views' zoom.
- Floats are sent without gzip; built assets are cached as immutable and gzipped.
- One error envelope; Host allow-list; POSTs must be JSON.

Planned: R2 (start of S5): a shared colour module on the GPU, colours keyed by category name.
R3 (before S6): views with several panels, linked view state, per-type view options.

## Known limits
- No downsampling yet (S5): very large embeddings are sent whole.
- The colour Worker receives copies of the codes (kept in the query cache for re-colouring); R2
  moves colouring to the GPU.
