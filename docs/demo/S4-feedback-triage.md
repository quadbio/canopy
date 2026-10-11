# S4 demo feedback: what happens with each item

Feedback: `feedback-S4.md` (2026-10-11, Chrome 154, MacBook Pro 13" M2). All 16 steps worked
(step 3 left blank); no scientific problem found.

| # | Feedback | Action | When |
|---|---|---|---|
| 1 | "Table" for SpatialData is not intuitive | A **?** next to *Table* explains it on hover; under the list, each table is described ("6.1 k rows · one per object in boundaries_cells"). A **?** also explains *Embedding*. | done (this batch) |
| 2 | Reasons and take-home of the "notes" unclear | Notes are now written in plain words, each with what it means and whether anything is needed; the heading says "(nothing to do)" or counts possible problems. Internal codes are no longer shown. | done |
| 3 | Step 12 ("four views") and step 14 ("reload") were ambiguous | Guide reworded: four *plots* in the centre; *reload the browser page*. | done |
| 4 | Closing a view and opening the dataset again starts from the defaults | Expected for now. Option: remember the last settings per dataset and table, and reuse them on Open. | your call (see below) |
| 5 | Not sure whether speed is from small data or optimisation | Added optional synthetic 1 M / 3 M cell datasets (`DBX_DEMO_LARGE=1`). Measured on this M2: open → picture in about 0.5 s for both; zoomed in, panning is smooth (~55 fps); with all 3 M cells on screen, ~15 fps (worse in small views, where points pile up on the same pixels). The fix is the planned S5 subset drawing when zoomed out. | measured now; fix in S5 |
| 6 | Only two colour maps for numbers; no palette choice for categories | Brought forward from S7 into R2 (start of S5, the colour rework): a palette choice for categories (default, colour-blind safe, many-category palette, the dataset's own colours) and more colour maps for numbers (viridis, magma, plasma, inferno, cividis, grey, plus a diverging map for values around 0). | S5 (R2) |
| 7 | Gene expression (.X) colouring missing | Planned as the main S5 feature. | S5 |

Also in this batch: the synthetic data generator now places embeddings as clusters, so large test
datasets look like real data.
