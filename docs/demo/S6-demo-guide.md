# S6 demo: spatial data, panels and your S5 wishes

Start, screen layout and how to report are as in the S4 guide (`S4-demo-guide.md`, sections 1,
2, 6 and 7). Stop a demo that is still running (Ctrl+C), then start again so the new version is
built:

```bash
cd /Users/hezhi/Tools/Projects/data_browser_server/dbx
pixi run -e dev demo        # then open http://127.0.0.1:8765
```

The large synthetic datasets are optional this time (`DBX_DEMO_LARGE=1` as before). Report in
`feedback-S6.md`. About 25 minutes.

Updated after the cold review (batch R6, 2026-10-12): steps 5, 8 and 9 changed slightly.

## Walk-through

| # | Do this | You should see |
|---|---|---|
| 1 | **example_with_layers.h5ad** (folder *generated*): the details list *Layers: counts*. Open it; colour by `sample`, **Palette** → *Colour-blind safe* | 80 different colours, and a note in the legend that in-between colours were added. |
| 2 | Colour by `cell_type`; click the dot next to *Monocyte* in the legend and pick a colour | The plot follows; the dot gets a ring. Close and reopen: the colour is kept. **reset colours** goes back. |
| 3 | **Gene** `Monocyte_m1`, then **Values from** → `counts` | The scale changes from log-normalised values to whole counts. |
| 4 | Open **spatial.h5ad**; **Embedding** → `spatial`; **Split by** → `embryo` | Three panels, one per embryo, with one legend, at one scale (**Panels**: *Same scale*). The tissue is the right way up (y downwards, as in scanpy). Zoom or pan one panel: all follow. |
| 5 | **Panels** → *Fill each panel*; then **Split by** → `pos`; then **Embedding** → `X_umap` with **Split by** `embryo` | Each embryo fills its panel. Then 51 small panels, still smooth. For the UMAP the panels show the same area (*Same area*), so where a population sits compares between embryos. |
| 6 | Click **202302171205_region_1…**, then **Convert for full speed** in the details | "Step n of 6" with the spatial steps (images, outlines, transcripts), then "✓ Converted" (about 15 s). |
| 7 | **Open spatial view** | The DAPI image with the cells filled in their `leiden_1.0` colours (the dataset's own); hover a cell: its cluster. |
| 8 | In **Image**, switch channels on and off, change a colour, drag the contrast (**auto** goes back) | The image changes at once; zooming in loads sharper tiles. |
| 9 | **Draw** → *Outlines*, then back to *Filled*; **Colour by** a gene (e.g. `SLC1A3`) | Cells stay coloured at every switch; outlines coloured by expression; the colour bar in the legend. |
| 10 | **Transcripts**: add two or three genes (e.g. `SLC1A3`, `GAD1`) | Each gene's molecules in its own colour; set **Cells** to *None* to see them alone. |
| 11 | **Coordinate system** → `rot` | Image, cells and transcripts turn together. |
| 12 | Back in the details: **Open embedding** for the same table | Its UMAP, with the same cluster colours. |

## What I would most like your judgement on

1. **Spatial view:** is the default (image + filled cells + dataset colours) a good start? What
   do you set first?
2. **Image controls:** enough (channel on/off, colour, contrast)? Wording?
3. **Transcripts:** how many genes at once do you need? One colour per gene, or by cell?
4. **Panels:** are "Split by" and the three **Panels** choices (same area, same scale, fill) what
   you expect for sections, samples and conditions?
5. **Your S5 wishes:** do the in-between colours and the hand-picked colours work the way you
   imagined?
