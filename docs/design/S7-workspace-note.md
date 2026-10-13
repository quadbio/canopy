# S7 rest: workspace, colours, search, filters (design note)

Date: 2026-10-13 · Status: **answered** (section 7: the owner's answers and the resulting plan)

The plan's S7 covers R3 (several datasets per session), R5 (palettes) and the search and filter
part of R4. The plot view (S7a) is done. This note lists what already exists, proposes the rest
in three parts, and asks the questions whose answers change the design. Nothing is built before
the answers.

## 1. What exists already

| Requirement | Already there | Missing |
|---|---|---|
| R3 several datasets | Up to 9 views on one canvas, each from any dataset; a view's panels (split by a column, a SpatialData's sections) with linked pan and zoom; settings remembered per dataset in the browser | More than 9 datasets at once (the lab has ~50 MERSCOPE regions); opening several in one step; changing a setting in all views at once; switching a view to the next dataset; saving or sharing a workspace (it lives in one browser only) |
| R5 palettes | Category colours by name (the same cell type has the same colour in every dataset); the dataset's own `uns` colours; 5 palettes, 8 colour maps; colours picked by hand (legend dot) | Picked colours are kept per view and dataset, not shared; no saved palette library, import or export; no custom range for numbers; zero not shown apart; no image export |
| R4 search, filters | Gene search inside a view (one gene, from the dataset's gene list); hiding categories in the legend | Several genes at once (a gene set score); the same gene in all views; finding datasets by gene; filters on numbers or gene values; selecting cells (lasso) |

## 2. Proposal A: workspace (R3)

- **A1 A view across datasets.** Select several datasets in the file browser (tick boxes, or
  "all in this folder") and open them as *one* view with a panel per dataset, as split panels
  are today: one set of settings (embedding or spatial layer, colour by, palette), linked pan
  and zoom, titles with the dataset names. One shared point budget keeps 50 regions smooth (each
  shows representative cells until zoomed in). This avoids 50 separate views and the 9-view
  limit, and makes "the same gene in all regions" one click.
- **A2 Apply to all.** In a normal view, "Apply to all views" next to Colour by (and palette,
  point size) sets the same choice in every open view where it exists. Views where it is missing
  say so.
- **A3 Next and previous dataset.** In a view, arrows switch to the next dataset of the same
  folder and keep the settings (for stepping through regions one by one).
- **A4 Saved workspaces.** Save the canvas (views, settings, colours) under a name, open it
  again later or from another browser. Two ways, see question 4: on the server (in `state_dir`,
  shared by everyone with the link) or as a file you download and load.

## 3. Proposal B: colours (R5)

- **B1 Shared picked colours.** A colour picked for a category applies to that category name in
  every view and dataset with the same column name (e.g. `celltype: T cell` → the same blue
  everywhere), instead of one view only. "Reset" goes back to the palette.
- **B2 Palette library.** Save the current category colours of a column as a named palette;
  apply it to any column; import and export as a small JSON file (and from a dataset's `uns`).
- **B3 Number colours.** A custom range (min and max typed or dragged on the colour bar) and an
  option to show zero (or values below a threshold) in grey, for genes. (Deferred from S5.)
- **B4 Image export.** A view (or the canvas) as PNG at a chosen resolution, with its legend;
  SVG for the plot view. (Part of R5; could also wait for a later stage.)

## 4. Proposal C: search and filters (R4)

- **C1 Gene set.** Type several genes (or paste a list); colour by their mean (of the log values)
  as one score. Missing genes are listed.
- **C2 Same gene everywhere.** Covered by A1/A2: one gene shown in all open views.
- **C3 Datasets with a gene.** In the file browser, search for a gene and see which datasets
  contain it (needs the gene lists of all datasets in the index: about 1 MB per 50 datasets).
- **C4 Filters.** Above the legend, a filter list: a category column (keep these categories),
  a number column or gene (keep cells between two values). All filters must hold (AND). Filtered
  cells are not drawn, and counts and plots use only the rest. Kept per view; with A1, for all of
  its datasets. Implemented on the GPU like hidden categories (one byte per cell).
- **C5 Lasso selection** (draw around cells, then filter to or compare them) is R8 "exploration
  analytics": proposed for later, unless you need it now.

## 5. Questions for the owner

1. **How many datasets at once**, typically? A few (2–6), or many (all ~50 regions of a run)?
   With many, A1 (one view, a panel per dataset) is the core of S7; with a few, separate views
   plus A2 may be enough.
2. **A1:** is one view with a panel per dataset (shared settings, linked pan and zoom) what you
   picture for "side by side"? Or should each dataset keep its own settings?
3. **A2:** should a change of Colour by apply to all views by default, or only when you click
   "Apply to all"?
4. **A4 saved workspaces:** on the server, shared by everyone who has the access link, or as
   files you download and load? (On the server, the dataset and column names of a workspace are
   stored in `state_dir`, readable only by the account that runs Canopy.)
5. **B1:** should picked colours follow the category name in every dataset with the same column
   name, or should some datasets keep their own (e.g. their `uns` colours)?
6. **C:** which matter most: a gene set score (C1), finding datasets by gene (C3), filters on
   numbers and genes (C4)? Is lasso selection (C5) needed in S7?
7. **B4 export:** needed now (e.g. for figures), or later?

## 6. Order and size

Recommended order, each part with tests and a short demo guide:

| Part | Content | Active days |
|---|---|---|
| S7b | A1–A3 (and A4 if on the server) | 3–4 |
| S7c | B1–B3 (B4 if wanted now) | 2–3 |
| S7d | C1, C3, C4 as chosen | 2–4 |

The plan had 4–6 days for S7 without the plot view; the answers above can shrink or grow this.

## 7. The owner's answers (2026-10-13) and the plan

Answers, in short:

1. Usually 1–3 datasets, as independent views (as now). Linked: up to 10–20 (spatial) datasets.
2. Views opened as now stay independent. **Linked** views are opened in one step: several
   datasets ticked in the dataset list, then "Open linked". Linked views share all settings;
   only the embedding (its name can differ between datasets) is per dataset.
3. In a linked group every change (colour, palette, …) applies to all of its views.
4. Later, the data folder will be open to everyone, to add data and share views: saved
   workspaces belong on the server, shared (a later stage).
5. Colours as 2 and 3: shared within a linked group. A column missing in a dataset colours its
   cells as NA. Preset colours (`uns`) of any linked dataset are checked first: if the same
   colour is given to different categories, or different colours to the same category, the
   colours are regenerated for the whole group; else they are used.
6. Gene sets, finding datasets by gene and filters are not crucial now. Lasso selection is
   wanted now, with the two-feature plot: the second feature becomes "in the selection or not".
7. Image export is needed, later.

What follows (replacing section 6):

| Part | Content |
|---|---|
| S7b | Linked groups: tick datasets, "Open linked"; one tile with a view per dataset, linked pan and zoom at one scale, one title and one legend; shared settings (per dataset: the embedding, and the spatial elements, whose names are per dataset too); colours over the group's categories with the preset-colour check, NA for a missing column, one number range for the group; up to 24 views per group, a group counts as one of the 9 tiles. |
| S7c | Lasso: draw around cells in a view; "Plot against: the selection" makes the plot's second feature "selected / not selected" (bars or violins, computed in the browser from what the view already holds). |
| later | Image export (B4); saved workspaces on the server, shared (A4); palettes library (B2); number range and zero in grey (B3); gene sets, datasets by gene, filters (C1, C3, C4); next/previous dataset (A3). |
