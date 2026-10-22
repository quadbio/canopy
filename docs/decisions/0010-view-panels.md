# 0010 Views with panels, linked pan and zoom (R3)

Date: 2026-10-11 · Status: accepted (R3, before S6)

## Context
Review S3–S4 (B-M4, B-m8, B-m9, B-m14) found that the view model was one view = one viewport.
That cannot express the section grid of S6 (F11c: one dataset split into up to ~50 sections that
share data, colours and legend and move together). The model also drew spatial coordinates
mirrored (fixed `flipY: false`), and fitted data under the legend. The S5 feedback adds chart
views (two-feature plots, start of S7), so the contract must not assume maps either.

## Decisions
- **Panels** (`views/registry.ts`):
  - A view's hook may return `panels: {id, title, layers, bounds, tooltip}[]` instead of
    `layers`/`bounds`/`tooltip`. `panelsOf(out)` turns a one-panel output into a single `main`
    panel, so existing views need nothing new.
  - Each panel is one deck.gl `OrthographicView` with id `<view>/<panel>`; its layers' ids start
    with `<view>/<panel>:` (`panelLayerId`), which `layerFilter`, tooltips and selection use
    (`ownerOf`).
  - All panels of all views stay in the single `Deck` (one WebGL context).
- **Layout** (`views/panels.ts`):
  - One panel fills the view's cell, with the title and legend floating over it.
  - Several panels form a grid below the title row and left of an open legend. Each panel has a
    light frame and its title (e.g. "embryo1 · 18 k").
  - There is one legend per view.
- **Linked pan and zoom:** `link: 'none' | 'zoom' | 'pan+zoom'` (default `pan+zoom`).
  - A moved panel's change is applied to the others relative to their own fitted state: the same
    zoom steps and the same distance on screen.
  - How panels are fitted (review S6, B-M5): `sameCoordinates` shows the union of all panels'
    data in every panel at one view state, so positions compare (a UMAP split by condition);
    `sameScale` fits each panel around its own cells at the smallest zoom, so sizes compare
    (sections); with neither, each panel fills its space. Linking works in all three.
  - The embedding's **Panels** setting offers *Same area* (default for embeddings),
    *Same scale* (default for coordinates named like `spatial`) and *Fill each panel*.
  - Panel bounds hold 99 % of a panel's cells in each direction (0.5–99.5 %), so a few stray
    cells do not shrink every panel (B-m7).
- **Per-output view options:** `flipY` (y grows downwards). The embedding view sets it for
  coordinates named like `spatial`, as scanpy and squidpy plot them.
- **Legend-aware fitting:** fitting leaves the title row and, when the legend is open, 250 px on
  the right free (`fitBounds` insets). Opening or closing the legend refits a view the user has
  not moved yet.
- **Refitting (B-m8):** a view may give a `fitKey` (what its data are, e.g. dataset and
  coordinate system). It refits when the key changes; more of the same data arriving (new
  bounds, same key) refits only a view the user has not moved. Without a key, every new
  `bounds` counts as new data (the embedding).
- **Ids:** view ids never contain `/` or `:`, panel ids never `:` (`panelKey` refuses them).
- **Layers coming back** (S6 feedback): deck.gl releases a layer's GPU buffers when the layer
  leaves the canvas. A view may hand back the same layer object later (its one-panel layer when
  "Split by" returns to None; a panel's layer when splitting by the same column again). The
  canvas replaces such a returning object with a fresh copy, so it is drawn with new buffers
  (it had stayed blank).
- **Hover:** the hover text is drawn by the canvas at the pointer (deck.gl's own tooltip was
  placed off screen on 2× displays); values follow each view's "Show values on hover" (on by default); deck.gl
  picks within 3 px, so small cells are easier to hit. Telemetry `hover` (per 10 s: pointer
  moves, cells hit, values shown) tells from the logs whether hovering finds cells.
- **Split by** (embedding):
  - Any categorical column with 2–64 categories (or a boolean) splits the view into one panel per
    category, in the column's own order (`.cat.categories`, owner 2026-10-15); missing values form
    an "NA" panel.
  - Number columns with 2–64 different values (time points, replicates) too (internal testing,
    2026-10-16): one panel per value, ascending, titled with the column ("day 3.5").
    `GET /api/datasets/{id}/obs/levels` lists them: every number column (at most 200) read once
    per dataset version and remembered, a sample of 100,000 values counted first; asked for by
    the selected view's settings and by a view split by numbers. The browser matches the served
    values to the levels by equality (the levels are the values as sent, float32 or float64).
  - Each panel draws its cells in the representative order of large datasets (ADR 0007/LOD), with
    its share of the view's point budget.
  - The panels together upload about one copy of the points (e2e).
- **Views must still re-render rarely** (B-m9). Pan and zoom states stay in the Canvas (deck.gl
  needs them as props). The per-view cells and legends are memoised and receive stable panel
  lists, and panel layers are rebuilt only when what they draw changes.
  - Measured with 51 panels (spatial.h5ad split by `pos`) in the software-WebGL test browser: the
    median frame while zooming and panning is 16.7 ms, the same as with one panel.
  - Moving view state out of React is not needed so far.

## Consequences
- S6 builds the section grid and the spatial view as panels; images and polygons get the same
  ids, linking and `flipY`.
- S7's Plot view can be a view type whose panel holds chart layers (or an overlay component);
  the canvas needs no change for that.
- The 9-view limit stays (visible: "At most 9 views…"); panels are limited per view (64).
- **Arranging views** (GitHub #1, 2026-10-22, option A): views keep a grid (one WebGL canvas, one
  viewport per panel; free-floating, overlapping windows were not built, as overlap would need
  stacking and picking on top of each other and cost drawing speed). A view's grip (⠿) drags it
  onto another view, which it replaces in the order (the others shift); Alt+←/→ from the
  keyboard; a plot below a view moves with it; in a linked group only among its members
  (`workspace.movedViews`, `placeOf`). *Columns* (Auto or 1–6, saved with the workspace) fixes
  the grid (`gridCells(…, columns)`), also inside a linked group, so rows stay rows as views open
  and close; back to Auto when the last view closes. A dataset can be opened several times at
  once ("Open several times", copies titled (1), (2), …), within the 9 views.

## Evidence
- Unit tests:
  - `panels.test.ts`: layout, relative linking, ids;
  - `layout.test.ts`: insets, flipped y;
  - `split.test.ts`: groups, order, NA, spatial axis, number levels;
  - `move.test.ts`: moving views, fixed columns, Auto again on an empty canvas.
- e2e "split by a column":
  - 3 panels with one legend, each drawing;
  - uploads ≤ 1.2 × one copy;
  - zooming one panel zooms all;
  - the fill fit, and *Same area* for the UMAP (identical view states);
  - back to one panel;
  - "split by numbers": the demo's `day` column, four panels;
  - `arrange.spec.ts`: open three times, three columns, drag, keyboard, reload, Auto again.
- Unit tests `panels.test.ts` (`fitLayout` in its three modes) and `split.test.ts` (robust
  bounds).
