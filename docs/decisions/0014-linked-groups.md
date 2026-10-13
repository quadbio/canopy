# 0014 Linked views of several datasets (S7b)

Date: 2026-10-13 · Status: accepted (the owner's S7 answers, `docs/design/S7-workspace-note.md` §7)

## Context
Usually 1–3 datasets are open as independent views, which already works. Sometimes 10–20
datasets (e.g. the regions of a MERSCOPE run) are to be compared side by side: the same
colouring, the same scale, moved together. The owner: such a *linked* opening is explicit
(datasets ticked together, opened at once); linked views share every setting except the
embedding, whose names can differ; a column missing in a dataset shows its cells as NA; preset
colours are used only when they agree across the datasets.

## Decisions
- **A group of views, not a new view type.** Each dataset stays an ordinary view (embedding or
  spatial: its own data loading, layers, level of detail); its `ViewSpec.group` names the group.
  Nothing in the view types changes but colouring (below) and a per-type `ownKeys` list.
- **Opening:** dataset rows have tick boxes (and folders "tick all"); with two or more ticked,
  *Open linked embeddings* (or *Open linked spatial views* when all are converted SpatialData)
  opens them as a group (at most `MAX_GROUP` = 24). Shared settings are the first dataset's
  defaults, coloured by a column all datasets have (else the first's default).
- **Shared settings** (`workspace.updateParams`/`setParams`): a change in one view is applied to
  every view of its group, except the type's `ownKeys` (embedding: `embedding`, `dims`). Spatial
  views share everything: their element choices fall back to each store's own when a name is
  missing. In a group, a saved choice missing in one dataset is not "corrected" for the group
  (no notice, nothing written back); that dataset shows NA (colour) or no split.
- **Layout** (`layout.tileCells`): a group takes one of the `MAX_VIEWS` (9) tiles; its views
  form a grid inside it below one title row (*Linked · N datasets*, Fit, Close) and left of one
  legend. Each view keeps a small title (its dataset) and × (remove it from the group; a group
  left with one view becomes an independent view).
- **Fitting and linking:** the group's views are fitted together at one scale (sizes compare)
  and pan and zoom together like the panels of one view (`combined` layout in the canvas; same
  relative linking as ADR 0010). The point budget is shared as for any views.
- **Colours** (`views/linked.ts`): each view publishes what its colouring found (categories and
  preset colours, or a number range) per colouring; every view of the group colours from all of
  them:
  - categories: the union of categories; preset colours used when they agree (never one colour
    for two categories, never two colours for one category), others get unused palette colours;
    on a disagreement palette colours for all, and the group's title says *colours regenerated*;
    other palettes colour the union by name;
  - numbers (columns, genes): one range over all views;
  - a dataset without the column or gene: its cells in the NA grey, its caption says so.
  - One legend for the group: the views' legends merged (counts added up). Hiding a category or
    picking a colour there applies to all (shared settings).
- **Not in groups yet:** the plot companion (a note says so); saving a group (saved workspaces
  are later, on the server).

## Evidence
- `src/views/linked.test.ts`: shared settings and own keys, tiles, restore of groups beyond 9
  views, a group of one, preset colours agreeing, conflicting (both kinds), union palettes,
  one range, merged legend.
- e2e `linked.spec.ts`: spatial.h5ad + dissociated.h5ad linked (one tile, one legend with the
  29 categories of both and added counts, preset colours kept, point size and hidden categories
  shared, the embedding per dataset, linked zoom, removing one); a dataset without the column
  shows NA. At the end of `browse.spec.ts` (it converts region_3): two MERSCOPE regions as
  linked spatial views at one scale, zoomed together (their `leiden` colours disagree:
  regenerated).
