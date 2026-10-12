# S7a demo: the Plot view (about 10 minutes)

Restart the demo (Ctrl+C, then `pixi run -e dev demo`) and open the link it prints. Report in
`feedback-S7a.md` (same form as before: step, ✓/✗, note).

| # | Do this | You should see |
|---|---|---|
| 1 | Click **example_with_layers.h5ad**, then **Plot** in its details | Violins of `total_counts` per `cell_type`, in the same colours as the embedding's legend. Hover a violin: cells, median, quartiles. |
| 2 | **Vertical (y)**: type `Monocyte_m1` in the gene search under it | Violins of the gene; above each, the share of cells above 0 (100 % for monocytes, a few % elsewhere). Tick **Log scale for y**. |
| 3 | In the legend, click a cell type | Its violin disappears (as in the embedding); click again to bring it back. Pick a colour for it in the legend: the violin follows. |
| 4 | **Vertical (y)** → `sample` | Composition: one bar per cell type, stacked by sample (80 samples); hover a piece. Untick **Shares of each bar** for cell counts. |
| 5 | **Horizontal (x)** → `total_counts`, y gene `Monocyte_m1` | A scatter with axes, coloured by cell type; zoom and pan move the axes; hover a point. |
| 6 | **Draw** → *Density map* | The same as a map of how many cells fall in each square (computed on the server; used automatically above 1 M cells). |
| 7 | **swap x and y**; open **spatial.h5ad** → **Plot** | Axes exchange. Another dataset's default plot. |

What I would like your judgement on: are the plot choices what you expect for each pair of
features? Are violins readable (widths, the share above 0)? Anything you plot often that is
missing (e.g. dot plots of many genes, several datasets in one plot)?
