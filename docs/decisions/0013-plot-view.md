# 0013 Plot view: two features as violins, composition, scatter or density (S7)

Date: 2026-10-12 · Status: accepted (start of S7, the owner's S5 wish)

## Context
The owner asked for plots of two features (S5 feedback): violins of a gene or a number per
category, stacked bars of two category columns, scatters of two numbers. They must stay fast for
atlases (10⁷ cells) reached over an SSH tunnel, and look like the rest (same category colours).
The full review (B-M6) noted that the view contract knew only zoomable maps.

## Decisions
- **A plot belongs to a view** (revised after the owner's first look, same day): not opened on
  its own from the dataset list, but by ticking *Plot against a second feature* (unticked by
  default) in an embedding's or spatial view's settings, under *Colour by*. A plot view opens
  beside it (`params.follows` = that view's id); its first feature (x) is always that view's
  colour (column or gene), its second is chosen in the same settings (*Plot against*). Unticking,
  or closing the view, closes the plot (`removeView`). A view of its own rather than a window
  floating over the map: it keeps the GPU scatter, the server summaries, its own pan and zoom
  and the canvas layout, and the map stays unobstructed. When the plot's categories are the
  view's colour column, that view's colour settings apply (palette, colours picked by hand,
  hidden categories) and its legend controls them; the plot shows none. A free plot (`follows`
  null) is still understood (restored workspaces), but no longer offered.
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
  logged as the `summary` stage (with the features' kinds, categories, read time and reply
  size). Measured on 10 M cells: violins (40 categories) 0.4 s (narrow category codes sort by
  radix; float32 values), bars 0.1 s, density 0.2 s. At most two summaries are computed at once,
  each reserving ~32 bytes per cell in the memory account.
- **At most 1 000 categories** (owner, R8): a categorical feature with more is neither offered
  for plotting nor for Colour by (a note says how many columns are left out), and the server
  refuses such summaries (409 `too_many_categories`) before allocating anything. Real
  annotations have far fewer; this catches a cell id column chosen by mistake. A text column
  read whole (its count unknown before) is refused the same way once read. Columns with a value
  per cell answer `too_many_values` (converting does not help, so it is not offered).
- **Scatter**: drawn by the GPU like the embedding (same colouring, hover, point size). Points
  sit in `[0, aspect] × [0, 1]` (aspect of the drawing area), so one zoom serves both axes; the
  axes overlay maps back to each feature's values (or log1p values).
- **Colours**: violins and bars are coloured by their category column through the shared
  colouring module; for the followed view's colour column with that view's own settings.
- **Violin outlines** cover only the bins a group has (no line through values it lacks). Widths
  are scaled to each group's busiest bin. For a gene, the cells at 0 (most cells) are left out
  of the outline, which they would flatten, and drawn as a flat bar at 0 whose width is the
  share of cells at 0; the share above 0 is printed above each violin. Categories follow the
  legends' natural order (2 before 10); long labels are shortened in the middle (full name on
  hover) and the axis makes room for them.
- **Hidden categories** of the followed view leave violins and, as bars, the composition too
  (owner, R8). A plot waiting for a choice shows a quiet instruction, not an error.
- **Density map** colours are on a log scale (cells per bin); the legend says so.
- **Log scales**: `log1p` per axis, applied on the server for summaries.

## Not yet
- Axes for violins and bars do not zoom; categories beyond the width scroll sideways (the
  value axis scrolls with them).
- Log scales on columns with negative values clamp them to 0 without a notice.
- Violin and bar hover uses the browser's own tooltip (a short delay), not the canvas's.
- Several datasets in one plot (S7's multi-dataset workspace).

## Evidence
- `tests/backend/test_summary.py`: violins, bars and densities equal straightforward pandas and
  numpy calculations (zeros of genes included, missing values counted), ETag, wrong kinds.
- `src/views/plot/plot.test.ts`, `axes.test.ts`: plot choice, feature kinds, defaults, scatter
  positions, ticks.
- e2e `plot.spec.ts`: unticked by default; ticked, a plot opens beside the embedding; violins in
  the embedding legend's colours, a category hidden there leaves, a gene's violins with shares
  above 0, composition, a change of Colour by changes the plot (scatter with axes and hover),
  density, unticking and closing the view close the plot.
- `state/workspace.test.ts`: a companion closes with its view, the selection moves to an open
  view.
