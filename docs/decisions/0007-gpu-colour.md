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
- **Palettes** for categories: automatic (the dataset's own `uns` colours when there is one per
  category, else the 54-colour default, else 256 colours), from the dataset, default (54),
  colour-blind safe (Okabe–Ito, 7), many (256, OKLCH hues by the golden angle). **Colour maps**
  for numbers from d3-scale-chromatic (the matplotlib maps exactly): viridis, magma, plasma,
  inferno, cividis, turbo, greys, blue–red (diverging, range made symmetric around 0).
- **Depth:** the canvas draws without depth testing by default; only the points layer turns it
  on, when "Higher values on top" is chosen (numbers and genes, default on). So no layer drawn
  later (outlines, transcripts, highlights in S6–S8) is ever hidden by it. *(R4, B-M3)*
- **While a colouring loads or fails** the view keeps drawing the last one (or the base colour),
  with a spinner, or the error and a Retry button; it never goes blank. *(R4, B-M2)*
- The shader is GLSL 3.00 (WebGL2, which deck.gl 9 requires anyway); a WebGPU device would need
  a WGSL version of the module.
- `colorBy` is `{kind: 'obs' | 'gene', name}`; saved strings from older builds are read as
  `obs`. Gene colouring is added later in S5.

## Evidence
- e2e (strengthened in R4 after review S5a B-M5): a gradient shows both ends of the colour map on
  screen (a one-colour plot shows neither); the same cell types (`celltype_harmonized`, at
  different positions in two example datasets) have the same colour; hiding a category removes
  its colour from the plot; a palette change changes the colours; neither sends point data
  (< 10 KB); higher values on top raise the share of top colours; a slow or failing gene keeps
  the plot drawn and Retry recovers.
- Unit tests: CPU reference of the shader lookup, ranges, slots, palettes (no black or grey).
- 3 M cells (M2, Chrome): open → ready 0.67 s; switching to a numeric column 0.4 s; frame time
  unchanged (zoomed-out drawing cost is overdraw, addressed by subset drawing in S5).

## Lessons
- An attribute added by an extension needs `stepMode: 'dynamic'` to be per point in instanced
  layers; without it every point read the first values and drew one colour, and the existing
  e2e checks ("something is drawn") did not notice. The e2e tests now count colours on screen.
