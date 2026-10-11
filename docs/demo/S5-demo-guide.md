# S5 demo: genes, colours, conversion and large datasets

Start, screen layout and how to report are as in the S4 guide (`S4-demo-guide.md`, sections 1,
2, 6 and 7). Start with the large synthetic datasets included:

```bash
cd /Users/hezhi/Tools/Projects/data_browser_server/dbx
DBX_DEMO_LARGE=1 pixi run -e dev demo        # then open http://127.0.0.1:8765
```

Report in `feedback-S5.md` (same format as before). About 20 minutes.

## Walk-through

| # | Do this | You should see |
|---|---|---|
| 1 | Open **spatial.h5ad**; in the right panel, **Gene**: type `lin28`, press Enter | The plot coloured by *Lin28a* (viridis), cells with higher values on top; the legend shows the gene and its scale (0 to the 99th percentile of expressing cells). Hover a cell: its value. |
| 2 | Type another gene (e.g. `Plek`), then untick **Higher values on top** | The plot switches in well under a second and never goes blank; without the tick, expressing cells are partly hidden by others. |
| 3 | **Colour by** `celltype_harmonized` | The dataset's own colours (from scanpy) are used: **Palette** says *Automatic*. |
| 4 | **Palette** → *Default*; open **dissociated.h5ad**, colour by `celltype_harmonized`, Palette *Default* | The same cell types have the same colours in both plots (e.g. Erythroid, NMP), even though they sit at different positions in the two datasets. |
| 5 | Try **Colour-blind safe** and **Many categories**; for a number column try the colour maps (incl. *blue–red*) | Palettes and maps change instantly. |
| 6 | Open **region_1**, table `table_cells`; pick a gene; **Values from** → `counts` | Gene values from the counts layer; hover shows whole numbers. |
| 7 | Close a plot, open the same dataset again | Your last settings come back; **Reset to defaults** (bottom of the panel) starts over. |
| 8 | Click **synthetic_3M_cells.h5ad**, then **Convert for full speed** in its details | A progress bar with the current stage, then "✓ Converted" (about 15 s on this Mac). |
| 9 | Open **synthetic_3M_cells.h5ad**; zoom in a few steps; then open **synthetic_1M_cells.h5ad** next to it | Next to the title: "1.0 M of 3.0 M cells"; panning is smooth. Zooming in draws more cells (up to 1.5 M). With two large views open they share the budget (e.g. "750 k of 3.0 M" and "250 k of 1.0 M"). Hide all categories but one: all its cells are drawn. **Show all cells** forces every cell. |
| 10 | Pick a gene in the 3 M view | Colours change within a fraction of a second (the converted copy reads each gene directly). |

## What I would most like your judgement on

1. **Gene colouring:** is the scale (0 to the 99th percentile of expressing cells) and "higher on
   top" what you expect from scanpy? Anything missing (e.g. setting the range yourself, which is
   planned with the workspace in S7)?
2. **Colours:** dataset colours first, *Default* for comparing datasets by name: is that clear?
3. **Conversion:** is it clear when and why to convert? Wording of the stages and messages.
4. **Large data:** does the 1 M-cell overview look representative? Is the switch when zooming
   noticeable?
