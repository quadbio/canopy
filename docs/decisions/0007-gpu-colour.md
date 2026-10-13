# 0007 Colouring on the GPU, colours by name

Date: 2026-10-11 · Status: accepted (review S3–S4 batch R2; S4 demo feedback)

## Context
Until S4, each view computed one RGBA colour per point in a Web Worker and uploaded it (4 bytes
per point). Every change of palette, colour map or hidden categories meant recomputing and
re-uploading all colours. Reviewer B (B-M3, B-M5, B-M2) asked for a GPU colour pipeline shared by
all views and for colours keyed by category name, before gene colouring arrives. The S4 demo
feedback asked for a choice of palettes for categories and more colour maps for numbers.

## Decisions
- **One float per point on the GPU** (`colour/gpu.ts`, `ValueColourExtension`, a deck.gl layer
  extension): a category code, or a number (missing numbers as a sentinel). A small RGBA lookup
  table (texture, 256 wide) turns it into a colour in the vertex shader; the range is a uniform.
  Palette, colour map, range and hidden categories change only the table or a uniform. Hidden
  categories have alpha 0 and are moved out of view, so they are neither drawn nor picked.
- The colour Web Worker is removed. Preparing values (Float32 copy, NaN → sentinel) and the
  robust range (1st–99th percentile, sampled to 200 k values) run once per loaded column.
- **Colours by name** (`colour/slots.ts`, revised in R4 after review S5a B-M1): a label's palette
  slot is a stable hash (FNV-1a) of its name; within one column, a label whose slot is taken
  moves to the next free slot (in natural order). The same cell type has the same colour in
  every view, dataset, browser and session with the same palette, whatever was opened before;
  nothing is stored. (The first version assigned slots in first-seen order and stored them, which
  made colours depend on history.) "Automatic" still uses a dataset's own `uns` colours when it
  has a full set: those are per dataset, and the Palette help says so.
- **Palettes** for categories: automatic (the dataset's own `uns['<col>_colors']` whenever they
  are usable, else the 54-colour default, else 256 colours), from the dataset, default (54),
  colour-blind safe (Okabe–Ito, 7), many (256, OKLCH hues by the golden angle). **Colour maps**
  for numbers from d3-scale-chromatic (the matplotlib maps exactly): viridis, magma, plasma,
  inferno, cividis, turbo, greys, blue–red (diverging, range made symmetric around 0).
- **More categories than colours** (S5 feedback, batch F3): a palette is extended by doubling,
  inserting the OKLCH midpoint (shorter hue arc, so mixes stay vivid) between neighbours, until
  every category has its own slot (`paletteSize(p, n)`, `slotColours`). The palette's own colours
  keep their places; no colour repeats. Slots are then hashed over the extended size, so colours
  by name agree across columns of a similar size (the same number of doublings), not with columns
  that fit the plain palette. The colour-blind safe palette is no longer fully safe once extended
  (the Palette help says so).
- **Colours picked by hand** (F3): clicking a legend dot opens the browser's colour picker; the
  colour is stored in the view's settings (`customColours[column][category]`, remembered per
  dataset) and replaces the palette's colour for that category. Only the lookup table changes.
- **Depth:** the canvas draws without depth testing by default; only the points layer turns it
  on, when "Higher values on top" is chosen (numbers and genes, default on). So no layer drawn
  later (outlines, transcripts, highlights in S6–S8) is ever hidden by it. *(R4, B-M3)*
- **While a colouring loads or fails** the view keeps drawing the last one (or the base colour),
  with a spinner, or the error and a Retry button; it never goes blank. *(R4, B-M2)*
- The shader is GLSL 3.00 (WebGL2, which deck.gl 9 requires anyway); a WebGPU device would need
  a WGSL version of the module.
- `colorBy` is `{kind: 'obs' | 'gene', name}`; saved strings from older builds are read as
  `obs`. Gene colouring is added later in S5.

- **The dataset's own colours come first** (user request, 2026-10-11): a column's
  `uns['<col>_colors']` is the default palette whenever it can be used, and a palette is made up
  only when it cannot. Usable means what scanpy and matplotlib accept: hex (`#rgb`, `#rrggbb`,
  `#rrggbbaa`, alpha dropped) or colour names (`red`, `lightgray`, `tab:blue`, `C0`); a longer
  list is used for its first colours (as scanpy does); a shorter one, or one with a non-colour,
  is not used and the server logs `uns_colors_ignored` with the reason. Plain text columns get
  their colours too (with their labels, in scanpy's alphabetical order).

- **Level of detail** (S5; revised in R5 after review S5b B-M1–B-M3, B-M7):
  - Large datasets (more than a quarter of the canvas budget, at most 200 k cells) are reordered
    once, "representative cells first": the server's stratified subset (`/subset`, every category
    of the main annotation kept) shuffled, then the other cells shuffled. Any prefix is a fair
    sample, so drawing fewer cells means drawing a shorter prefix of the same GPU buffers:
    changing how many are drawn uploads nothing (e2e: < 20 KB while zooming).
  - The canvas shares one budget of points on screen (1 M; `?lod=<n>` overrides it) among its
    views in proportion to their cells (`views/lod.ts` `allowances`).
  - The budget adapts to the machine (first host test, 2026-10-13: frames of ~110 ms while
    moving 1.7 M cells on an M2 at 2560 × 1440). Every 30 frames drawn while the canvas is
    *dragged* with its whole budget on screen (wheel zoom draws at the wheel's pace, so it does
    not count; S7b–c review B-M2): when the median is above 1.5 × the display's frame interval
    (at least 25 ms; the interval is measured at start, so 30 Hz displays are not "slow") the
    budget shrinks in proportion (to at most half per step, not below 200 k); at about the
    display's rate it grows back by a quarter, up to 1 M. A lowered budget is kept for the
    browser session only; `lod_budget` events log changes.
  - Hover picking draws all points once more (~10 ms at 1 M on an M2): while the pointer moves,
    at most one pick per 100 ms, and one where it stops. The `frames` event also reports the
    frames drawn while moving and what was shown (views, cells, budget, device pixels).
  - A view draws as many cells as keep about its share on screen, up to 1.5 × its share (points
    off screen still cost time). Hidden categories count as not on screen, and the cap grows
    with them: isolating one population draws all of it.
  - Points grow a little (at most 1.5×) when fewer are drawn, so the density looks similar. The
    view header says "1.0 M of 3.0 M cells"; "Show all cells" overrides.
  - Until the order is known a large view draws nothing new (no full upload first).
  - Spatially chunked drawing (only the chunks in view) comes with S6; until then a deep zoom
    into a very large dataset shows a dense sample, not every cell.

## Evidence
- e2e (strengthened in R4 after review S5a B-M5): a gradient shows both ends of the colour map on
  screen (a one-colour plot shows neither); the same cell types (`celltype_harmonized`, at
  different positions in two example datasets) have the same colour; hiding a category removes
  its colour from the plot; a palette change changes the colours; neither sends point data
  (< 10 KB); higher values on top raise the share of top colours; a slow or failing gene keeps
  the plot drawn and Retry recovers.
- Unit tests: CPU reference of the shader lookup, ranges, slots, palettes (no black or grey).
- Level of detail, 3 M cells (M2, Chrome, Metal): zoomed out 18 ms per frame (1 M drawn), first
  zoom step 29 ms, further steps 17–20 ms (1.5 M drawn); four views of 3 M + 1 M + 3 M + 1 M cells
  21 ms per frame (72 ms before the shared budget).
- 3 M cells (M2, Chrome): open → ready 0.67 s; switching to a numeric column 0.4 s; frame time
  unchanged (zoomed-out drawing cost is overdraw, addressed by subset drawing in S5).

## Lessons
- An attribute added by an extension needs `stepMode: 'dynamic'` to be per point in instanced
  layers; without it every point read the first values and drew one colour, and the existing
  e2e checks ("something is drawn") did not notice. The e2e tests now count colours on screen.
