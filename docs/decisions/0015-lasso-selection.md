# 0015 Lasso selection, plotted against the other cells (S7c)

Date: 2026-10-13 · Status: accepted (the owner's S7 answer 6)

## Context
The owner wants lasso selection now, with the two-feature plot: the plot's second feature
becomes "is the cell in the selection or not". Filters and gene sets wait.

## Decisions
- **Drawing:** a **lasso** button in the title row of a view that can select (embedding, spatial; not
  yet in linked groups) turns on the lasso for one drawing: drag around cells; Shift while
  releasing adds to the current selection; Esc stops. The points are turned into the panel's
  coordinates with deck.gl's viewport (`unproject`), in the panel the drawing started in.
- **What is selected** (`ViewOutput.lasso.select`): every cell of the view's table inside the
  polygon, not only the cells drawn at the current level of detail; cells hidden in the legend
  are left out. Embedding: the positions it holds (in a split view, only that panel's cells).
  Spatial: the cells' centres (`centroid`, or `center` for circles) and `row` of the cells
  element, read once, the polygon taken into the element's own coordinates (inverse affine);
  only when the cells are the rows of the view's table.
- **Kept** per view in the page (`views/selection.ts`: a mask of one byte per cell, the count,
  the polygons); not saved with the workspace (it can be millions of cells), dropped when the
  view closes. The outline stays drawn while the view shows the same coordinates (`lasso.key`:
  embedding and dimensions, split; store, system and element). The title row shows
  "N selected ×" (× clears).
- **Plot:** *Plot against* offers **Lasso selection** in a plot below a view. Its first feature
  is the view's colour, coloured as in the view:
  - categories → composition: two bars, the selected cells and the others, stacked by the
    view's categories (hidden ones left out);
  - a number or gene → two violins (selected in the lasso's colour, the others grey), with the
    share above 0 for genes.
  Computed in the browser from the values the view already holds (same form as the server's
  summaries: 64 bins, quartiles, mean): no request, and a new lasso updates the plot at once.
  Without a selection the plot shows a quiet instruction.
- Telemetry: `lasso` (time, cells, selected, whether added).
- R9 (cold review): pointer positions are taken from the canvas's corner (they were 30 px above
  the pointer); the stroke is kept as it is drawn (a fast release kept its last part); a lasso
  started outside every panel keeps lasso mode on with a hint; lasso mode ends with its view;
  Esc in a field is the field's; a selection read for a view that closed meanwhile is dropped;
  outlines are made once per selection.

## Not yet
- Lasso in linked groups; a selection as a filter or as a new column; saving selections.

## Evidence
- `src/views/selection.test.ts`: inside/outside (concave), hidden cells and panel cells left
  out, adding without double counting, polygon rows to table rows, both summaries against
  hand-computed values.
- e2e `plot.spec.ts` "lasso": the path under the pointer; instruction before a selection; a
  lasso selects part of the cells; two bars; Shift adds; a gene gives two violins; a lasso
  around the whole view selects all 8 000 cells; clearing.
