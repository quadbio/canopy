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

## Revised after the cold review (R9, 2026-10-13)
- **A group and independent views never share the canvas** (owner): *Open linked* needs an
  empty canvas, and while a group is open no dataset opens on its own. Both places say so and
  offer **Close all** (which keeps the ticks). The group fills the canvas; restoring an older
  save with both keeps the group alone; independent views are restored up to 9 tiles. A plot
  below its view takes no tile.
- **Scale per coordinates** (owner: option 2): views showing the same coordinates (`scaleKey`:
  the embedding and its dimensions, or spatial coordinates) are fitted at one scale; a view
  showing other coordinates fills its own space. Only views showing the same coordinates pan and
  zoom together (owner, S7b demo: a UMAP and spatial coordinates moved together, and in
  opposite directions on y); a view switched back joins their scale and moves with them again. When one view gets new data (e.g. its embedding changes) only it is refitted:
  at the scale of its peers, then joined to where the user moved the group (no one's zoom is
  lost).
- **Default colour:** the column most of the datasets have (ties: as the first dataset would
  choose). The group legend ends with *NA: N cells (k datasets without …)*; its counts stay
  while a view reloads. **Reset to defaults** in a group gives the group's shared defaults.
- **Opening:** details and meta of the ticked datasets are read six at a time; one that fails
  (or cannot be shown) is left out and named, the others open.
- **Colours** are worked out once per group and colouring (`groupScheme`), and the same object is
  handed out while nothing changed, so views do not rebuild colour tables when another view
  reports the same facts.

## Revision after host test 3 (2026-10-14, owner)
- **Lock.** The group's title row has a lock: locked (the default) as above; unlocked, every view
  zooms and pans on its own (also views showing the same coordinates). Locking again links the
  views showing the same coordinates and returns them to one fitted scale. The setting
  (`unlocked`) is shared by the group and saved with the workspace.
- **Shift-click ticking.** A shift-click on a tick ticks (or unticks, following the clicked one)
  every dataset of the same list (folder or search results) between it and the last tick
  clicked.
- **Convert ticked.** When ticked spatial datasets have no converted copy, the ticked panel
  offers to convert them all. The browser hands them to the server in order; when its queue is
  full (`jobs.max_queued`, `queue_full`) it tries again every 10 s, for as long as the page is
  open. At most `jobs.max_running` run at a time, as for single conversions.
- **Review H3.** The lock is one toggle, "Move each view on its own", pressed while unlocked; a
  group locked again by any route (also Reset to defaults) returns to one fitted scale. Views
  needing a fit together when no other view of their coordinates has one are fitted together
  (a wheel while a group loads). *Close all* with a group or several views open asks first, in
  place; it keeps the dataset details. Publishes of a group's colourings are applied once per
  frame. *Convert ticked* names conversions that failed and can stop waiting.
- **Full review 2.** The settings of a group are the group's: titled "Linked · N datasets", with
  Colour by, gene search and Split by offering every member's columns and genes ("in k of N").
  Split by in a group stays within 96 panels in all. Member titles leave out the prefix all
  members share. A group's settings are not remembered per member.
- **Gear.** Every view (and a group) has a gear in its title row that shows its settings,
  unfolding the settings panel when it was folded away (clicking the title still works).

## Evidence
- `src/views/linked.test.ts`: shared settings and own keys, a group only on an empty canvas and
  nothing beside it, plots take no tile, restore (a group alone, 9 tiles), a group of one,
  preset colours agreeing, conflicting (both kinds), union palettes, one range, merged legend.
- e2e `linked.spec.ts` (R9): opening blocked beside a group; a view switched to other
  coordinates while the other keeps the user's zoom; three datasets coloured by the column two
  have, the third NA with the legend's NA line; *Open linked* after **Close all** (ticks kept).
- e2e `linked.spec.ts`: spatial.h5ad + dissociated.h5ad linked (one tile, one legend with the
  29 categories of both and added counts, preset colours kept, point size and hidden categories
  shared, the embedding per dataset, linked zoom, removing one); a dataset without the column
  shows NA. At the end of `browse.spec.ts` (it converts region_3): two MERSCOPE regions as
  linked spatial views at one scale, zoomed together (their `leiden` colours disagree:
  regenerated); since host test 3 both are converted with *Convert all 2*, unlocked (one zooms
  alone) and locked again (both back at the fitted scale, linked).
- Host test 3: `src/ui/ticks.test.ts`, `src/ui/convertMany.test.ts` (order, a full queue retried,
  a refusal named); e2e `linked.spec.ts` shift-click in the search results (tick and untick a
  range); e2e `plot.spec.ts` the plot's gear with the settings panel folded away.
