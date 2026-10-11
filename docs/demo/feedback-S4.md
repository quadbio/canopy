# S4 demo feedback

Date:
Browser (Chrome / Safari / Firefox + version): Chrome 154.0.8037.98
Screen (e.g. MacBook 14", external 27"): MacBook Pro 13-inch, M2, 2022

## Walk-through (guide section 3)

Mark each step: ✓ worked as described · ✗ did not (add a finding below) · – skipped

| # | Step | ✓ / ✗ / – | Note |
|---|---|---|---|
| 1 | Folders and datasets listed | ✓ | |
| 2 | Search | ✓ | |
| 3 | Details of spatial.h5ad |  | |
| 4 | Open → UMAP by cell type | ✓ | |
| 5 | Zoom, pan, tooltip | ✓ | |
| 6 | Legend: hide/show, fold | ✓ | |
| 7 | Colour by category / number / colour map | ✓ | |
| 8 | Embedding X_pca with chosen dimensions | ✓ | |
| 9 | Point size, opacity | ✓ | |
| 10 | Second dataset side by side | ✓ | |
| 11 | SpatialData table view | ✓ | I assume the "table view" here means to choose between table_cells and table_tiles. The term is not intuitive to normal users. An explanation, for example, a question mark icon with a floating text field when mouse moving on top to explain what it means could help |
| 12 | Four views, selecting views | ✓ | I assume the four view mean the search, dataset info, visualization and selection. That works. It also works to select different opened dataset views |
| 13 | Closing a view | ✓ | |
| 14 | Reload keeps the views | ✓ | I assume the "reload" here means reloading/refreshing the website. In that case it works. If it means closing the view and re-open the same data, then the view goes back to the default. |
| 15 | Light / dark theme | ✓ | |
| 16 | Refresh the index | ✓ | |

## Overall impression (a few sentences)

- Scientific correctness: No issue is found
- Speed and smoothness: Fast enough, but not fully sure whether it is because of the small data sizes or optimization
- Layout and look: Looks clean and elegant. It is a nice layout and style
- Wording: Generally speaking no problem. Some technical terms may be not intuitive for less computational people (e.g. "TABLE" for the spatial data); The reasons and take-home for the "notes" section in the dataset information is not clear.
- Expected but missing: Only two preset color palettes for continuous metadata information, and no way to change the palettes for the categorical information; currently the visualization function for features in the expression matrix (.X) is still missing.

## Findings

<!-- Copy this block once per finding. Kind: bug | looks wrong | slow | wording | wish.
     Priority: must fix | should fix | nice to have. -->

### (short title)
- Kind: 
- Priority:
- Where:
- Steps:
- Expected:
- Saw:
- Time:
- Screenshot:
