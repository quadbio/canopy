# S6 demo feedback: triage (2026-10-12)

From `feedback-S6.md`. Steps 1, 2, 5, 6, 8–12 worked; scientific correctness, speed and look fine.

| Finding | Kind | Done |
|---|---|---|
| Blank view after **Split by** → None (stays blank; also when splitting by the same column again) | bug, high | Fixed. Cause: the view handed deck.gl back a layer object deck.gl had already released (no GPU buffers). The canvas now replaces any returning layer object with a fresh copy (ADR 0010). Browser test: split, None, split again, None: drawn each time |
| No hover text (spatial and embedding views) | bug, high | Fixed (second round). The demo's logs showed the values were produced for most pointer moves (e.g. 139 of 173), so the text existed but was not visible: on a 2× (Retina) screen deck.gl placed its tooltip about 300 px above the screen. The canvas now draws the hover text itself at the pointer, kept inside the canvas. New browser test at 2× pixel ratio checks it appears next to the pointer (the old tests only checked it existed). Also: 3 px picking tolerance; telemetry `hover` |
| Unclear that SpatialData must be converted before the spatial view | wording | A note in the details of an unconverted SpatialData store: the spatial view needs a converted copy, "Convert for full speed" first; "Open" shows the embeddings without it |
| Wish: hover information in every view, with an option to turn it off | wish | "Show values on hover" in the colour settings of every view (on by default); the canvas honours it for all view types |
| Found on the way: the memory estimate for the MERSCOPE region was 2.3× its measured peak, so on a laptop its conversion could wait for free memory | fix | Recalibrated (bench/README.md): 1.62 GB estimated, 1.36 GB measured |

Step 3 (values from the counts layer) and step 4's second half are covered by the fix above;
step 3 was not tried.
