# S7b–c demo feedback: triage (2026-10-13)

From `feedback-S7b.md`. All steps but 13 and 19 worked (both blocked by the first finding).

| Finding | Cause | Done |
|---|---|---|
| "Convert for full speed" fails for region_1 and region_3 ("the file changed while it was being converted") | The project folder was renamed (`dbx` → `canopy`, 2026-10-10). Dataset ids hold the real path; the index kept the old ids because a rescan skipped every dataset whose file times had not changed, and moving a folder keeps them. The conversion compares the current id with the indexed one and refused. The h5ad examples live in a folder that did not move. The same would happen on a host whose root moved (a new mount point; the second host test used a new root path). | A rescan also compares the id (the real path): a moved root is inspected again. The demo's next start (or **Refresh**) fixes the index. Test `test_a_moved_root_is_inspected_again`; checked on a copy of the demo index. |
| One scale per embedding: views with different embeddings still move together, and a spatial and a UMAP view move in opposite directions on y | Pan and zoom were linked across the whole group. | Only views showing the same coordinates (embedding and dimensions; spatial) move together; one switched back joins their scale and moves with them. e2e. |
| The details of a dataset clicked before stay beside the ticked datasets | Ticking did not clear the selection. | Ticking replaces the details with the ticked panel. e2e. |
| The lasso button should be the usual lasso icon | — | An icon (loop of rope); the label stays for screen readers. |

Open: one legend beside 10–20 linked views (to try with more datasets).
