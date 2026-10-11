# S5 demo feedback: what happens with each item

Feedback: `feedback-S5.md` (2026-10-11). 9 of 10 steps worked. Step 6 (values from a layer)
could not be tried. No scientific problem was found, and it ran smoothly on the 1 M and 3 M
synthetic datasets. Atlas-scale tests come later on the host.

| # | Feedback | Action | When |
|---|---|---|---|
| 1 | Set colours per category directly | Click a colour dot in the legend to open the browser's colour picker. The chosen colour replaces the palette's for that category of that column. It is remembered with the dataset's settings, and a ring marks it. **reset colours** in the legend title goes back to the palette. | done (batch F3) |
| 2 | More categories than palette colours: interpolate instead of repeating | Each palette is extended by doubling: a colour taken halfway between each pair of neighbours (in OKLCH, along the shorter hue path, so mixes stay vivid instead of grey). This repeats until every category has its own colour. The palette's own colours stay; no colour repeats; neighbours can look alike, and the legend says so. Example: 80 categories with the 7 colour-blind safe colours become 112 slots, 80 different colours. | done (F3) |
| 3 | No example dataset with layers | A generated example is added to the demo: **example_with_layers.h5ad** (root *generated*, 8 000 cells × 600 genes). Its matrices belong together, as in a scanpy file: `layers["counts"]` holds integer counts and X is log1p(counts / total × 10 000). Marker genes are named after their cell type (e.g. `Monocyte_m1`). It also has an 80-category column (`sample`, no saved colours) to try item 2. The details panel now has a **Layers** row, and the Gene help names the layers. Note: the *region_1* cell table, which step 6 used, does have a `counts` layer; "Values from" only appears after a gene is picked, which made it easy to miss. | done (F3) |
| 4 | Plots of two features (scatter, violin, stacked bars) | A new kind of view, beyond the plan's stage 1 (it was "exploration analytics, R8, nice to have"). Design and placement are proposed below; **your choice is needed**. | **decided: after S6, as the start of S7** |

## How to try F3 (5 minutes)

```bash
cd /Users/hezhi/Tools/Projects/data_browser_server/dbx
DBX_DEMO_LARGE=1 pixi run -e dev demo        # writes the generated example on first start
```

1. Click **example_with_layers.h5ad**. The details list *Layers: counts*. Open it.
2. **Colour by** `sample`, **Palette** → *Colour-blind safe*. The result is 80 different colours,
   and the legend notes that in-between colours were added.
3. **Colour by** `cell_type`. Click the dot next to *Monocyte* and pick a colour. The plot
   follows, and the dot gets a ring. Close the plot and open the dataset again: the colour is
   kept. **reset colours** brings back the palette.
4. **Gene** `Monocyte_m1`, then **Values from** → `counts`. The scale changes from log-normalised
   values (up to about 6) to whole counts, and hovering shows whole numbers.

## Item 4: plots of two features (proposal)

**What.** A **Plot** view next to the embeddings. You choose two features, each either an obs
column or a gene (from X or a layer), and the plot type follows from their kinds:

| Features | Plot | Options |
|---|---|---|
| number × number | scatter; a 2-D density above about 1 M cells | colour by a third feature (as in the embedding), log axes |
| number × categories | violin per category, with median and quartiles | share of cells above 0 (for genes), order of categories, hidden categories left out |
| categories × categories | stacked bars of proportions | proportions or counts, which column forms the bars, colours from the stacked column's palette |

**How it stays fast on atlases.** The server summarises; the browser draws small results:
- **Violins:** one histogram per category over a shared range, plus quartiles and the share of
  non-zero values.
- **Stacked bars:** a table of counts.
- **Density:** binned counts.

Each summary is a few KB, uses the same gene and column reads as colouring (so a converted dataset
answers in milliseconds), and costs one pass over the values (10 M cells is well under a second).
Small scatters reuse the embedding's point drawing and level of detail. Every summary request is
logged like the gene requests, for the host tests.

**Effort.** About 4–6 active days:
- a server endpoint with tests;
- the Plot view, with SVG for violins and bars, and the points layer for scatters;
- e2e tests and a demo step.

The R3 view contract (several panels in one view) should then also allow views that are not
maps. That costs little if it is known now.

**Where it could go.** I recommend **(a)**:
- **(a) After S6 spatial, as the first part of S7 (workspace)** *(recommended)*. S6 holds the main
  risk left for the host tests: images, polygons and transcripts at scale. Plots are low risk and
  build on what exists.
- **(b) Right after R3, before S6.** You get them sooner, and the host test of spatial data moves
  back by about a week.
- **(c) After stage 1 (stage 3).** Stage 1 stays as planned.
