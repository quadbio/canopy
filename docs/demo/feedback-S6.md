# S6 demo feedback

Date:
Browser (Chrome / Safari / Firefox + version):
Screen:

## Walk-through (S6 guide)

Mark each step: ✓ worked as described · ✗ did not (add a finding below) · – skipped

| # | Step | ✓ / ✗ / – | Note |
|---|---|---|---|
| 1 | More categories than palette colours | ✓ | |
| 2 | Pick a colour in the legend | ✓ | |
| 3 | Values from the counts layer | | |
| 4 | Split by embryo, linked zoom | ✓✗ | The split-by function itself works, so as the linked zoom function. Issues happen when resetting the split-by back to None |
| 5 | Own scale; 51 panels | ✓ | |
| 6 | Convert region_1 | ✓ | |
| 7 | Spatial view: image + coloured cells | ✓✗ | The demo guide claimed "hover a cell: its cluster". I see nothing when hover a cell |
| 8 | Image channels, colours, contrast | ✓ | |
| 9 | Outlines coloured by a gene | ✓ | |
| 10 | Transcripts of a few genes | ✓ | |
| 11 | Coordinate system `rot` | ✓ | |
| 12 | Embedding of the same table | ✓ | |

## Overall impression (a few sentences)

- Scientific correctness: No issue
- Speed and smoothness: Fast and smooth with the example datasets
- Layout and look: Looks good
- Wording: Generally good. For spatial data, it is unclear that one should do "Convert for full speed" first and only then the spatial view can be opened. There should be a note on that
- Expected but missing: It would be good to have the cell information on the selected feature shown when hover the cell, regardless what kinds of view. It would also be good to have the option to turn it off.

## Findings

<!-- Copy this block once per finding. Kind: bug | looks wrong | slow | wording | wish.
     Priority: must fix | should fix | nice to have. -->

### Blank view when split by metadata and then back to None
- Kind: When spliting view of a value by certain categorical variable, if the split-by is set back immediately to None, the view becomes blank and remains blank even if it choose to show a different feature. The only way to fix it is to set the split-by again, change the feature to show to a different one, and then set the split-by to None again. It happens to different datasets.
- Priority: High
- Where: Frontend (view)
- Steps: Open a dataset -> At "SPLIT BY", choose any variable -> At "SPLIT BY", choose None
- Expected: The view should show the feature on the full dataset
- Saw: The view becomes blank
- Time:
- Screenshot:

### Claimed hover behavior not found
- Kind: In the Demo guide, it is said that "hover a cell: its cluster". I see nothing when hover a cell
- Priority: Low to medium
- Where: Frontend (view)
- Steps: Open a spatial dataset -> Convert for full speed -> Open spatial view -> Hover a cell
- Expected: The cluster information is shown when hover
- Saw: Nothing appear
- Time:
- Screenshot:

