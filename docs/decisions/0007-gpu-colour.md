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
- **Colours by name** (`colour/slots.ts`): each category label gets a palette slot the first
  time it is shown and keeps it (workspace `labelSlots`, at most 5,000 labels, kept in the
  browser). Within one column labels never share a slot while free slots remain, so a column
  is never less distinct than with positional colours. Known slots that collide in a column are
  not overwritten.
- **Palettes** for categories: automatic (the dataset's own `uns` colours when there is one per
  category, else the 54-colour default, else 256 colours), from the dataset, default (54),
  colour-blind safe (Okabe–Ito, 7), many (256, OKLCH hues by the golden angle). **Colour maps**
  for numbers from d3-scale-chromatic (the matplotlib maps exactly): viridis, magma, plasma,
  inferno, cividis, turbo, greys, blue–red (diverging, range made symmetric around 0).
- `colorBy` is `{kind: 'obs' | 'gene', name}`; saved strings from older builds are read as
  `obs`. Gene colouring is added later in S5.

## Evidence
- e2e: points are drawn in many colours (category) and a gradient (numbers), measured on screen;
  the same cluster has the same colour in two datasets; palette change + hiding a category upload
  < 10 KB (positions 62 KB, values 31 KB not re-sent); hidden categories not drawn or hovered.
- Unit tests: CPU reference of the shader lookup, ranges, slots, palettes (no black or grey).
- 3 M cells (M2, Chrome): open → ready 0.67 s; switching to a numeric column 0.4 s; frame time
  unchanged (zoomed-out drawing cost is overdraw, addressed by subset drawing in S5).

## Lessons
- An attribute added by an extension needs `stepMode: 'dynamic'` to be per point in instanced
  layers; without it every point read the first values and drew one colour, and the existing
  e2e checks ("something is drawn") did not notice. The e2e tests now count colours on screen.
