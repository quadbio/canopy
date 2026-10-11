# S4 demo: how to try it and how to send feedback

This demo runs **on this Mac only**, with the example datasets in `../example_datasets`. Nothing
here touches the host, so the data policy does not limit what you send back: screenshots and
log files are fine, and I can read the logs on this Mac myself.

Time needed: about 20–30 minutes.

---

## 1. Start it

In a terminal:

```bash
cd /Users/hezhi/Tools/Projects/data_browser_server/dbx
pixi run -e dev demo
```

The first start takes about a minute. It unpacks the two SpatialData examples once and builds
the web app. When you see

```
3/3 Starting the server.
    Open      http://127.0.0.1:8765
```

open **http://127.0.0.1:8765** in Chrome (Safari and Firefox should also work; Chrome is the
reference). Leave the terminal open; **Ctrl+C** stops the server.

If it says the port is in use, an old server is still running: stop it, or start on another port
with `DBX_DEMO_PORT=8766 pixi run -e dev demo` and open `http://127.0.0.1:8766`.

## 2. The screen

```
┌──────────────────────────────────────────────────────────────────────────┐
│ dbx  data browser                         Indexed 14:02  Refresh  Theme  │
├───────────────┬─────────────────────────────────────────┬────────────────┤
│ Search…       │                                         │ View settings  │
│ ▾ examples    │        views (1–9, in a grid)           │ Embedding      │
│   dissociated │                                         │ Colour by      │
│   spatial     │                          ┌──────────┐   │ Point size     │
│ ▾ extracted   │                          │ legend   │   │ Opacity        │
│   region_1    │                          └──────────┘   │                │
│   region_3    │                                         │                │
│───────────────│                                         │                │
│ details +Open │                                         │                │
└───────────────┴─────────────────────────────────────────┴────────────────┘
```

- **Left:** your data folders (`examples` and `extracted`), a search box, and, at the bottom,
  the details of the dataset you clicked, with the **Open** button.
- **Centre:** the open views. All of them are drawn in one canvas.
- **Right:** the settings of the selected view (it has a blue border).

## 3. Walk-through (tick each step in the feedback file)

| # | Do this | You should see |
|---|---|---|
| 1 | Look at the left panel | Two folders, both open, four datasets with their cell counts (16 k, 52 k, 6.1 k, 7.8 k). "Indexed hh:mm" at the top. |
| 2 | Type `region` in the search box, then clear it | Only the two region datasets, then all four again. |
| 3 | Click **spatial.h5ad** | Details at the bottom left: type h5ad, 22 MB, 52 k × 351, matrix csr, its embeddings. |
| 4 | Click **Open** | A view appears with a small spinner, then the UMAP coloured by `celltype_seqfish`, with a legend (categories and cell counts). It should take a few seconds at most. |
| 5 | Scroll to zoom, drag to pan; hover over cells | Smooth movement; a tooltip with the cell's cell type. |
| 6 | In the legend, click a few cell types; then **hide all** / **show all**; click the legend title | Clicked types fade in the legend and vanish from the plot; the legend folds up and opens again. |
| 7 | Right panel: **Colour by** → `embryo`, then `total_counts`, then **Colour map** → magma | New colours each time; for `total_counts` a colour bar with the value range. |
| 8 | **Embedding** → `X_pca`, then pick dimensions, e.g. x = 3, y = 4 | The plot changes to the chosen PCA components and is refitted. |
| 9 | Move **Point size** and **Opacity** | The points change as you drag. |
| 10 | Click **dissociated.h5ad**, **Open** | A second view; both share the canvas side by side and each fits its own data. |
| 11 | Click **202302171205_region_1…**; under **Table** choose `table_cells`; **Open** | A third view: the UMAP of the segmented cells of this spatial dataset, coloured by its cluster column (e.g. `leiden_1.0`). A spatial dataset can hold several tables, one per kind of object (here cells and tiles); hover the **?** next to *Table* for the explanation. |
| 12 | Open **region_3** too, so that **four plots** are open; then click each plot's title | The centre shows the four plots in a 2 × 2 grid; legends start folded in small plots (click the legend title to unfold); the right panel shows the settings of the plot whose title you clicked. |
| 13 | Zoom into one view, then close another with **×** | The grid rearranges; the view you zoomed keeps its place, untouched views refit. **⤢** (next to ×) fits a view to its data again. |
| 14 | Reload the browser page (Cmd+R) | The same plots come back, with your settings. Closing a plot with × and opening the same dataset again also brings back its last settings; **Reset to defaults** (bottom of the right panel) starts over. |
| 15 | Click **Theme** (auto → light → dark) | The whole app switches between light and dark. |
| 16 | Click **Refresh** (top right) | "Scanning…" briefly, then a new "Indexed" time. |

## 3b. Optional: larger datasets

The examples are small (at most 52 k cells), so they say little about speed. To try 1 M and 3 M
cells (synthetic: 40 clusters, made up on this Mac, about 1.1 GB, generated once in ~1 minute):

```bash
DBX_DEMO_LARGE=1 pixi run -e dev demo
```

A third folder, **synthetic**, appears. Measured on this MacBook (M2, Chrome): open → picture in
about 0.5 s for both; panning is smooth when zoomed in, but with all 3 M cells on screen it drops
to about 15 frames per second (S5 adds drawing a representative subset when zoomed out).

## 4. Not built yet (please don't report these as missing)

- Drawing a subset of cells when zoomed out: **S5**. (Gene colouring and conversion are in: the
  *Gene* box in the right panel, and **Convert for full speed** in a dataset's details.)
- **Spatial view** (tissue images, cell outlines, transcripts) and the **section grid**: **S6**.
- Editing single colours, gene search, filters: **S7**.
- Polished visual design: stage 3. This demo has a clean, plain look on purpose. Your opinion on
  layout and feel is still very welcome now.

## 5. What I would most like your judgement on

1. **Scientific correctness.** Do colours, categories and counts match what you see in scanpy or
   your usual tools for the same file? Is the UMAP oriented as you expect (y axis up, as in
   scanpy)?
2. **Feel.** Is zooming and panning smooth with 52 k cells? Is the time from **Open** to the
   picture acceptable?
3. **Layout and look.** Panel widths, font sizes, legend position (it can cover points),
   light/dark, how the file browser shows datasets.
4. **Words.** Any label, message or setting name that is unclear.
5. **Expectations.** Anything you reached for and did not find in a first embedding view.

## 6. How to report

Use `docs/demo/feedback-S4.md` (a template is there): fill it in and tell me "feedback is in the
file", or paste the same entries into the chat. One entry per finding:

```markdown
### Legend covers the cells on the right
- Kind: looks wrong            (bug | looks wrong | slow | wording | wish)
- Priority: should fix         (must fix | should fix | nice to have)
- Where: legend, embedding view
- Steps: open spatial.h5ad
- Expected: the legend beside the plot, not on top of it
- Saw: it hides the Erythroid cluster
- Time: about 14:35            (so I can find it in the log)
- Screenshot: ~/Desktop/legend.png   (optional; Cmd+Shift+4 takes one)
```

Tips:

- **Time** matters most for bugs and slowness. Every action and its duration is in the server
  log (`.cache/demo/logs/`), including timings the browser sends (open, load, colouring, first
  draw). With the time, I can find exactly what happened, without you copying anything.
- If a **red error message** appears in the app or a view, a screenshot of it is enough.
- For **slowness**, say which action and roughly how long it took ("Open took ~5 s",
  "panning stutters with 4 views").
- Optional, for things that break: Chrome menu **View → Developer → JavaScript Console**, and a
  screenshot of any red lines.

## 7. If something goes wrong

| Problem | What to do |
|---|---|
| Page is blank or old | Hard reload: Cmd+Shift+R |
| "Port … already in use" | Stop the other server, or use `DBX_DEMO_PORT=8766` |
| A view stays on the spinner | Note the time and dataset, close the view, report it |
| You want a clean start | **Close all** in the header |
| The terminal shows an error and the server stopped | Copy the last ~20 lines of the terminal into the report |

When you're done, stop the server with **Ctrl+C**.
