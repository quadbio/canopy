# S7b–c demo: linked views of several datasets, lasso selection (about 15 minutes)

Restart the demo (Ctrl+C, then `pixi run -e dev demo`) and open the link it prints. Report in
`feedback-S7b.md` (step, ✓/✗, note).

| # | Do this | You should see |
|---|---|---|
| 1 | Tick **spatial.h5ad** and **dissociated.h5ad** (the boxes left of the names) | A panel *2 datasets ticked* with **Open linked embeddings** and **Clear**. One tick alone cannot be opened linked. |
| 2 | **Open linked embeddings** | One tile *Linked · 2 datasets* with a view per dataset, at one scale, and one legend: `celltype_harmonized` with the categories of both (29) and their counts added up, in the datasets' own colours (they agree). The ticks are cleared. |
| 3 | Click a dataset's title; change **Point size**, then **Palette** | The settings say *Linked with 1 other dataset*. Both views change. |
| 4 | Hide a category in the legend; pick a colour for another | Both views follow. |
| 5 | **Embedding** → `spatial` (in spatial.h5ad's settings) | Only that view changes: the embedding is chosen per dataset. |
| 6 | Zoom and pan in one view; then the tile's ⤢ | The other moves along; ⤢ fits both again. |
| 7 | **Colour by** a gene | Both coloured by the gene on one colour scale (one range for both). |
| 8 | × on one dataset's title, then **Close all** | The other stays as an ordinary view. |
| 9 | Tick **spatial.h5ad** and **example_with_layers.h5ad** → open linked | The second has no `celltype_harmonized`: its cells are grey, its caption says *NA*. |
| 10 | In *extracted*, **tick all** (both regions; convert region_3 first if needed) → **Open linked spatial views** | Both regions side by side at one scale, moved together, one legend. Their own `leiden_1.0` colours disagree, so the title says *colours regenerated* and palette colours are used for both. |

What I would like your judgement on: is ticking and *Open linked* the right way in? Is one
legend beside the views right for 10–20 datasets (each view's title is its dataset)? Should
the views keep their own fitted scale instead of one scale? Anything else that should stay per
dataset besides the embedding?

## S7c: lasso selection (about 5 minutes)

| # | Do this | You should see |
|---|---|---|
| 11 | Open **example_with_layers.h5ad**; tick **Plot against a second feature**; **Plot against** → *Lasso selection* | The plot asks you to draw around cells with the lasso (**lasso** in the view's title row). |
| 12 | Click **lasso** and drag around a group of cells | An orange outline; the title row says *N selected ×*. The plot shows two bars: the selected cells and the others, stacked by cell type in the legend's colours. |
| 13 | Click **lasso** again and draw elsewhere holding **Shift** when you release | The selection grows (both outlines shown). |
| 14 | **Colour by** a gene (e.g. `Monocyte_m1`) | Two violins: selected (orange) and the others (grey), with the share above 0. |
| 15 | Hide a cell type in the legend, draw a new lasso over it | Hidden cells are not selected. |
| 16 | Try it in a spatial view (region_1) | The cells whose centres are inside are selected. |
| 17 | × next to *N selected* | The selection and its outline go; the plot waits again. |

Judgement wanted: should a selection also become a filter (show only these cells) or a column
you can colour by? Is "selected vs the others" the comparison you need, or selected vs all?
