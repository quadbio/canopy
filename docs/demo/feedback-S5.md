# S5 demo feedback

Date:
Browser (Chrome / Safari / Firefox + version):
Screen:

## Walk-through (S5 guide)

Mark each step: ✓ worked as described · ✗ did not (add a finding below) · – skipped

| # | Step | ✓ / ✗ / – | Note |
|---|---|---|---|
| 1 | Colour by a gene | ✓ | |
| 2 | Switch genes; higher values on top | ✓ | |
| 3 | Dataset colours by default | ✓ | |
| 4 | Same colours by name across datasets | ✓ | |
| 5 | Palettes and colour maps | ✓ | It works in terms of selecting different color maps and palettes. It is not fully in the way I imagine though. I expect that (1) one can specify colors per category directly; (2) in case of more categories than possible colors in the palette, instead of repeating the same colors multiple times, one should do color interpolation to fill in colors so that there will be no identical colors even though some colors may be very similar. |
| 6 | Values from a layer | ? | I assume this means whether one can check the values from different layers. I couldn't check that as there is no example dataset with additional layers rather than .X |
| 7 | Remembered settings, Reset | ✓ | |
| 8 | Convert for full speed | ✓ | |
| 9 | Large dataset: subset when zoomed out | ✓ | |
| 10 | Genes on the converted 3 M dataset | ✓ | |

## Overall impression (a few sentences)

- Scientific correctness: No issue
- Speed and smoothness: Very smooth, so as for the two larger synthesized data. Though the two synthesized datasets are indeed much smaller than the usual atlas-level datasets as they only include 300 genes. Here, synthesizing even larger data is not necessary as it's not practical to do that in this development computer and real test on loading the read atlas-level data will be done later during the real test run on the actual hosting server
- Layout and look: Looks clean and elegant
- Wording: No issue
- Expected but missing: So far so good. It would be probably good to have more visualization supports than just plotting features on a given embedding, with particular focus on checking relationship between two features (e.g. scatter plot when two numeric features are selected, violin plot when one numeric feature and one categorical feature is selected, stacked bar plot showing proportions when two categorical features are selected)

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
