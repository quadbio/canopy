# 0013 Plot view: two features as violins, composition, scatter or density (S7)

Date: 2026-10-12 · Status: accepted (start of S7, the owner's S5 wish)

## Context
The owner asked for plots of two features (S5 feedback): violins of a gene or a number per
category, stacked bars of two category columns, scatters of two numbers. They must stay fast for
atlases (10⁷ cells) reached over an SSH tunnel, and look like the rest (same category colours).
The full review (B-M6) noted that the view contract knew only zoomable maps.

## Decisions
- **View contract** (`views/registry.ts`):
  - `ViewOutput.content`: a chart drawn in the page (SVG) instead of map layers, shown in the
    view's cell below its title and left of an open legend; nothing pans or zooms.
  - `PanelOutput.overlay(state, rect)`: drawn over a panel and redrawn as it pans and zooms
    (the scatter's axes).
  - `ViewOutput.margins`: room kept free when fitting (axes); `Insets` has all four sides.
  - The canvas publishes each view's panel size (`useLod().size`) for charts that fill it.
- **Which plot** follows from the features' kinds: categories × number → violins (either
  order); categories × categories → stacked bars (rows: x, stacks: y); number × number → a
  scatter up to `SCATTER_MAX` = 1 M cells, else a density map ("Draw" overrides). Genes are
  numbers; boolean and text columns are categories.
- **Server summaries** (`GET /api/datasets/{id}/summary`, `canopy.data.summary`): violins as
  per-category histograms over one shared range (64 bins) with quartiles, mean and the share above
  0; bars as a count table; densities as a 128 × 128 grid. Counts and statistics only, never
  values; one pass over the same cached column and gene reads as colouring; cached, with an ETag;
  logged as the `summary` stage. Measured on 10 M cells: violins (40 categories) 1.5 s, bars
  0.1 s, density 0.2 s.
- **Scatter**: drawn by the GPU like the embedding (same colouring, hover, point size). Points
  sit in `[0, aspect] × [0, 1]` (aspect of the drawing area), so one zoom serves both axes; the
  axes overlay maps back to each feature's values (or log1p values).
- **Colours**: violins and bars are coloured by their category column through the shared
  colouring module, so palette, dataset colours, colours picked by hand and hidden categories
  are those of the embedding.
- **Violin widths** are scaled to each category's busiest bin, leaving out a spike of zeros
  (most cells for a gene), which would flatten the rest; that spike is drawn full width and the
  share of cells above 0 is printed above each violin.
- **Log scales**: `log1p` per axis, applied on the server for summaries.

## Not yet
- Axes for violins and bars do not zoom; categories beyond the width scroll sideways.
- Violin and bar order follows the column's category order (the legend sorts by name).
- Several datasets in one plot (S7's multi-dataset workspace).

## Evidence
- `tests/backend/test_summary.py`: violins, bars and densities equal straightforward pandas and
  numpy calculations (zeros of genes included, missing values counted), ETag, wrong kinds.
- `src/views/plot/plot.test.ts`, `axes.test.ts`: plot choice, feature kinds, defaults, scatter
  positions, ticks.
- e2e `plot.spec.ts`: violins in the legend's colours, a hidden category leaves, a gene's
  violins with shares above 0, composition, scatter with axes and hover, density.
