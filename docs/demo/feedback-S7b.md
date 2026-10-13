# S7b–c demo feedback (linked views, lasso selection)

Date: Oct 13, 2026
Browser (Chrome / Safari / Firefox + version): Chrome 154.0.8037.98
Screen: Macbook 12' retina

## Walk-through (S7b–c guide)

Mark each step: ✓ worked as described · ✗ did not (add a finding below) · – skipped

| # | Step | ✓ / ✗ / – | Note |
|---|---|---|---|
| 1 | Tick two datasets: the *ticked* panel; one tick cannot open linked | ✓ | |
| 2 | Open linked embeddings: one tile, one scale, one legend (29 categories, own colours) | ✓ | |
| 3 | Point size and palette change in both; *Linked with 1 other dataset* | ✓ | |
| 4 | Hide a category, pick a colour: both follow | ✓ | |
| 5 | Embedding per dataset | ✓ | |
| 6 | Zoom and pan together; ⤢ fits both | ✓ | |
| 7 | A gene on one colour scale | ✓ | |
| 8 | No Open beside linked views; Close all offered | ✓ | |
| 9 | Another embedding fills its own space; the others keep the zoom | ✓ | |
| 10 | × leaves an ordinary view; Close all | ✓ | |
| 11 | Open linked waits while a view is open; ticks stay | ✓ | |
| 12 | Three datasets: most-shared column; NA caption and legend line | ✓ | |
| 13 | Two regions as linked spatial views; *colours regenerated* | - | "Convert for full speed" didn't work for region_1 and region_3. It output error "The file changed while it was being converted; refresh the index and try again". The function works for the h5ad files  |
| 14 | Plot against: Lasso selection; the instruction | ✓ | |
| 15 | Lasso: outline under the pointer; *N selected*; two bars | ✓ | |
| 16 | Shift adds to the selection | ✓ | |
| 17 | Gene colour: two violins | ✓ | |
| 18 | Hidden cells are not selected | ✓ | |
| 19 | Lasso in a spatial view | - | The spatial view cannot be opened as the "Convert for full speed" function didn't work for the two spatial data. |
| 20 | × clears the selection; the plot waits again | ✓ | |

## Judgement asked for

- Ticking and *Open linked* as the way in: yes this is what I expected
- One legend beside 10–20 linked views: i couldn't test this right now as there is not yet so many datasets to open and test. we keep this open.
- One scale per embedding (step 9): this doesn't work as expected. after changing the embedding of one dataset, the other linked dataset didn't resume the scale, but all the grouped datasets' embeddings are still coupled together when moving or zooming. And when region_1 and region_3 are grouped opened, if one dataset uses "spatial" embedding while the other one uses "X_umap", the two views are still coupled, and move to the opposite direction when moving on the y-axis
- Anything else that should stay per dataset besides the embedding: embedding is good enough for now
- A selection as a filter or as a column to colour by: the current mode is good
- "Selected vs the others", or selected vs all: selected vs. the others is good

## Overall impression (a few sentences)

- Scientific correctness: No issue
- Speed and smoothness: No issue
- Layout and look: At the dataset selection panel, if firstly click at one dataset, and after the dataset information window appears, tick multiple datasets for grouped selection, the "Open linked embedding" window appears but the dataset information window stays, and still shows the firstly clicked dataset. this can be confusing. Besides, it is probably better to replace the "lasso" button by the typical lasso icon.
- Wording: No issue
- Expected but missing: No issue

## Findings

<!-- Copy this block once per finding. Kind: bug | looks wrong | slow | wording | wish.
     Priority: must fix | should fix | nice to have. -->

### "Convert for full speed" didn't work for region_1 and region_3
- Kind: "Convert for full speed" didn't work for region_1 and region_3. It output error "The file changed while it was being converted; refresh the index and try again". The function works for the h5ad files
- Priority: High
- Where: Dataset information window
- Steps: Click at region_1 dataset -> click "Convert for full speed"
- Expected: Conversion
- Seen: Failed by outputing error "The file changed while it was being converted; refresh the index and try again"
