# S7a demo: plotting two features (about 10 minutes)

Restart the demo (Ctrl+C, then `pixi run -e dev demo`) and open the link it prints. Report in
`feedback-S7a.md` (same form as before: step, ✓/✗, note).

The plot belongs to the view you open: its first feature is always the view's **Colour by**
(a column or a gene); the second is chosen under **Plot against a second feature**.

| # | Do this | You should see |
|---|---|---|
| 1 | Click **example_with_layers.h5ad** → **Open**. In the view's settings, under Colour by, tick **Plot against a second feature** | Unticked before. A plot opens in the bottom third of the embedding's tile; a **Plot against** field appears in the embedding's settings (the settings stay on the embedding). |
| 2 | **Plot against** → `total_counts` | Violins of `total_counts` per `cell_type` (the embedding's colour), in the embedding's legend colours. The plot has no legend of its own. Hover a violin: cells, median, quartiles. |
| 3 | **Plot against**: type `Monocyte_m1` in the gene search under it | Violins of the gene; above each, the share of cells above 0 (100 % for monocytes, a few % elsewhere). The cells at 0 are a flat bar at the bottom, as wide as their share; the outline above shows the expressing cells. |
| 4 | In the embedding's legend, click a cell type; then pick a colour for one | Its violin disappears with its cells; click again to bring it back. The violin follows the picked colour. |
| 5 | **Plot against** → `sample` | Composition: one bar per cell type, stacked by sample (80 samples); hover a piece. Hide a cell type in the embedding's legend: its bar goes too. |
| 6 | Embedding **Colour by** → `total_counts`; **Plot against**: gene `Monocyte_m1` | The plot's first feature follows: a scatter of total_counts × Monocyte_m1 with axes, filling the plot's width; zoom and pan move the axes; hover a point. |
| 7 | Click the plot's title to see its own settings: **Draw** → *Density map*; tick a log scale | Its settings show the first feature (not choosable) and the plot's options. The same cells as a map of how many fall in each square (computed on the server; used automatically above 1 M cells). |
| 8 | Back on the embedding, untick **Plot against…**; tick it again, then close the embedding (×) | Unticking closes the plot; closing the embedding closes its plot too. Also try it in a spatial view (**spatial.h5ad**). |

A column with more than 1 000 categories (e.g. a cell id) is not offered for Colour by or Plot
against; a note under the field says how many columns are left out.

What I would like your judgement on: is the plot below its view (a third of its tile) right? Are the plot choices what you expect for each pair of features? Are violins readable
(widths, the share above 0)? Anything you plot often that is missing (e.g. dot plots of many
genes, several datasets in one plot)?
