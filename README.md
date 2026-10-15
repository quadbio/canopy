<img src="docs/assets/canopy-logo.png" alt="Canopy logo" width="160" align="right" />

# Canopy: lab single-cell and spatial data browser

Canopy lists the datasets on the lab's data share and opens them in the web browser: h5ad,
AnnData-zarr and SpatialData stores (zarr format 2 and 3), from a few thousand to millions of
cells. It runs as one program on a shared Linux host, under the account of the person who
installs it, and lab members reach it through an SSH tunnel with a link. No data leaves the host.

**Status:** stage 1 is complete (steps S0–S9), tested three times on the lab's host with real
data (October 2026) and reviewed in two full cold reviews and ten fix batches. Next is
**Checkpoint A**: the lab uses it for real, then decides the scope of stage 3.

## What it does

- **Find datasets.** A folder tree and search over the configured data folders, with each
  dataset's cells, genes, columns, embeddings and tables, and plain-language notes on anything
  unusual in the file. Rescans pick up new and changed files.
- **Embeddings.** Any `obsm` embedding (and any two of its dimensions), coloured by a column or a
  gene; palettes (the dataset's own colours, a 54-colour default, colour-blind safe, 256 colours),
  colour maps for numbers, colours picked by hand, categories hidden from the legend, values on
  hover, and *Split by* a column into panels.
- **Genes.** Search by symbol or id; values from `X` or any layer; whole-number counts exact.
- **Spatial sections.** OME-Zarr images by channel (tile by tile), cell outlines filled or
  outlined, transcripts per gene; coordinate systems and affine transforms from SpatialData.
- **Plots.** Each view can plot its colouring against a second feature: violins (with the share
  of cells above 0), composition bars, scatter or density maps.
- **Lasso.** Select cells in an embedding or a section and compare them with the others in the
  plot.
- **Linked views.** Up to 24 datasets side by side as one tile: shared settings, one legend with
  conflict-checked colours, NA where a dataset lacks a column, and zoom and pan together (or
  each on its own with the lock).
- **Conversion.** *Convert for full speed* writes a copy laid out for the browser (genes by
  gene, image pyramids, outline blocks, transcripts by gene) in a background job; many datasets
  at once with *Convert all N*.
- **Up to 9 views** on one canvas; the workspace and each dataset's last settings are kept in the
  browser.
- **For the owner:** an admin link and a revocable member link, `selfcheck` for the installation,
  `cache prune` for disk space, and privacy-filtered diagnostics bundles for the developer.

How loading and drawing are built for large data, compared with cellxgene, cirrocumulus, SCope
and Vitessce, is the section "Built for scale and speed" of the architecture map
(`docs/architecture/index.html`).

## Implementation status

| Step | What | State |
|---|---|---|
| S0–S2 | Environment, inspector and synthetic data, data contract for converted stores | done |
| S3–S4 | Crawler and catalog, dataset API, the first embedding view | done |
| S5 | Genes, the AnnData converter, background jobs, memory guard | done |
| S6 | SpatialData: converter and spatial view | done |
| S7 | Workspace and colours, plots, linked views, lasso | done (filters and gene sets deferred) |
| S8 | Diagnostics: logs at three privacy levels, bundle, selfcheck, analyser | done |
| S9 | Release archive, host guide, Linux container trial | done |

Measured on the lab's host (three tests): about 16 ms frames while moving with 3.4 M cells;
10–15 ms with 19–21 linked spatial sections; views ready in 0.17 s (median); gene requests
answered in at most 225 ms; a lasso over 1.65 M cells in 23–165 ms; a 55 GB h5ad converted in
16 minutes. The step-by-step log is `PROGRESS.md`; the decisions behind each part are in
`docs/decisions/`.

Known gaps: SpatialData segmentation masks (labels) are converted but not drawn yet, and all
z-planes of transcripts are drawn together (no z filter yet).

## Roadmap

After Checkpoint A, stage 3 is planned to start with groundwork that the next features need:

- pan and zoom kept in a store of their own (for saved views and image export), with the fitting
  logic split out of the canvas and tested on its own;
- an export hook for every view type (legends rendered as SVG);
- workspaces with ids, revisions and per-user keys, ready to be saved on the server;
- a test fixture with 24 linked datasets, and GPU timings on real hardware.

Then, as the lab decides:

- **image export** of views and legends for figures;
- **a palette library** and a lab-wide colour registry; a custom number range with 0 in grey;
- **multi-user access**: sign-in through the institute's gateway, who may see which data,
  uploads, and shared saved workspaces (a design note comes first);
- **labels and a z filter** for SpatialData;
- **gene sets and filters**, and differential expression, if users ask for them;
- design polish and short user tests with lab members.

## Setting up on a server

The full manual, with the reasons behind each step, is **`docs/host/host-guide.md`** (also
inside every release). In short, as a normal user on the host:

```bash
# 1. pixi, the package manager (no root)
export PIXI_HOME=/scratch/$USER/pixi PIXI_CACHE_DIR=/scratch/$USER/pixi-cache
curl -fsSL https://pixi.sh/install.sh | bash

# 2. the release (built on the developer's Mac with `pixi run -e dev release`)
mkdir -p /scratch/$USER/canopy && cd /scratch/$USER/canopy
sha256sum -c canopy-*.tar.gz.sha256 && tar -xzf canopy-*.tar.gz && cd canopy-*/
pixi install --locked

# 3. the config: data folders, cache, state (local disk), memory
cp config.host.example.toml config.toml && nano config.toml
pixi run --frozen canopy selfcheck --config config.toml

# 4. start it in tmux; the admin link is printed there
tmux new -s canopy
pixi run --frozen canopy serve --config config.toml
```

From a laptop: `ssh -N -L 8765:127.0.0.1:8765 you@host`, then open the link.

- **Access.** By default (`server.auth = "token"`) the server prints its **admin** link when it
  starts; `canopy link --config …` prints it again, and `canopy link --member --config …` the
  **member** link for lab members (everything but cancelling a conversion). Opening a link stores
  its token in a cookie; requests without one are refused, and the log records which kind of link
  each request came with (`access`). `--new` replaces a link's token, which revokes the old link
  within seconds. Whoever has either link sees every dataset in the data roots. Data inside the
  roots is never followed outside them: HDF5 external storage, external links and virtual
  datasets, and symlinks out of a zarr store are refused (`links_outside`).
- **Files.** Converted copies, index and logs are readable only by that user
  (`server.file_access = "private"`; `"group"` adds the Unix group).
- **State on a local disk.** `state_dir` holds the index and job databases, the server lock, the
  privacy salt and the access tokens (SQLite and file locks need a local disk); `cache_dir` the
  converted copies (may be network storage). Keep all folders in the config absolute, so a new
  release finds them.
- **Upgrades** keep the index, links and converted copies (host guide §11). `canopy stop` stops
  the server; conversions that were running start again with it.

## Documentation

| Where | What |
|---|---|
| `docs/host/host-guide.md` | Installing and running on a shared host, memory sizing, links, upgrades, disk clean-up, diagnostics |
| `docs/architecture/index.html` | Architecture map, generated from the code: parts, paths, scale and speed, every module, the HTTP API |
| `docs/decisions/` | Architecture decision records (0001–0015) |
| `contract/SPEC.md` | The data contract of converted stores |
| `docs/demo/` | Demo walk-throughs and the owner's feedback per step |
| `PROGRESS.md` | What was done when, review rounds and host tests |

## Quick start (development)

```bash
pixi install --locked -e dev           # exact locked environment (Python, Node, libraries)
pixi run -e dev install-hooks          # pre-commit hook runs `check`
pixi run -e dev fe-install             # frontend packages from pnpm-lock.yaml
cp config.example.toml config.toml     # adjust data roots
pixi run -e dev build-frontend         # static frontend served by the backend
pixi run serve --config config.toml    # prints the access link (http://127.0.0.1:8765/?token=…)
```

During frontend work, `pixi run -e dev fe-dev` starts Vite on :5173 with `/api` proxied to the backend.

## Demo, release and architecture map

- `pixi run -e dev demo`: the app on this Mac at http://127.0.0.1:8765 with the example
  datasets. Walk-throughs: `docs/demo/` (S4 browsing, S5 genes, S6 spatial, S7a plots, S7b linked
  views and lasso).
- `pixi run -e dev release`: `dist-release/canopy-<version>-<commit>.tar.gz` and its `.sha256`
  (needs a clean working tree); `bash scripts/linux-trial.sh` installs it in a fresh Ubuntu
  container and checks it.
- `pixi run -e dev architecture`: regenerates `docs/architecture/index.html`.

## Configuration, logs and privacy

- Every command takes `--config FILE` (or the `CANOPY_CONFIG` environment variable). Unknown keys,
  missing data roots and a cache inside a data root are errors, so typos never fall back silently.
- Logs go to `[logging] dir` of the config, else to `~/.cache/canopy/logs` (or `$XDG_CACHE_HOME`),
  never to the current folder.
- `[logging] privacy` sets what log files may contain (they are meant to be copied off the host):
  - `standard` (default; the host template sets `strict`): names are kept in their own fields (`path`, `root`, `column`, …);
    free text (exception and warning messages, third-party log lines) has quoted text and
    absolute paths replaced by tokens and is cut at 300 characters. Values are never logged.
  - `strict`: every name field becomes a salted token and free text is dropped (uvicorn's
    fixed-template lines are redacted as in `standard`); exceptions keep their type and code
    locations only.
  - `debug`: everything, including full tracebacks; for the developer's own machine only.
- `canopy inspect PATH --level strict` replaces user-chosen names with tokens salted by a random
  secret created once per installation (`<state_dir>/.canopy_salt`, or `~/.cache/canopy/.canopy_salt`
  without a config). The salt stays on the host; never copy it off with diagnostic bundles.

## Catalog

The server indexes the data roots at start-up, every `[catalog] rescan_minutes`, and on demand
(`POST /api/catalog/refresh`). `canopy catalog scan [--full] --config FILE` does one scan from the
command line (it refuses while a server runs: press Refresh instead). The index is
`<state_dir>/catalog.sqlite`. It can be rebuilt by a scan, but `cache prune` decides what to keep
from it: never prune with a missing or new index (prune refuses).

- Recognised: `.h5ad` files, AnnData-zarr and SpatialData-zarr stores. Zarr stores are never
  descended into; hidden entries and `[catalog] ignore` patterns are skipped.
- Rescans inspect only datasets whose size/modification time changed. A dataset's version is its
  root's name, its path inside the root and that stamp, so moving or remounting a whole root keeps
  every converted copy. A root that cannot be read (e.g. an unmounted share) keeps its entries
  until it is back.
- Symlinks leaving a data root are skipped; a dataset's path is checked against its root again
  every time it is opened. The API takes dataset ids and root-relative folder keys only.
- API: `GET /api/catalog/roots`, `/tree?root=&path=`, `/search?q=`, `/datasets/{id}`, `/status`;
  `POST /api/catalog/refresh?full=`.

## Dataset API and stores

- `GET /api/datasets/{id}/meta?table=`: cells, genes, obs columns (kind, categories, colours
  from `uns[<col>_colors]`: hex or matplotlib colour names, as `#rrggbb`; a longer list is cut to
  the categories, a shorter one is not used and logged as `uns_colors_ignored`), embeddings,
  matrices. `table` picks a SpatialData table.
- `GET /api/datasets/{id}/obs?column=` and `/obsm?key=&dims=0,1`: binary little-endian arrays
  (`x-canopy-dtype`, `x-canopy-shape`, `x-canopy-kind` headers; weak ETag, 304 before any read). Category
  and boolean codes are int8/16/32 with -1 for missing; numbers float32 (NaN for missing), or
  float64 for whole numbers above 2^24 (ids, large counts), so they stay exact. Floats are not gzipped (they
  barely compress); JSON and codes are. `GET …/obs/labels?column=` gives the labels behind the
  codes, also for plain string columns.
- `GET /api/datasets/{id}/genes?table=`: gene names (the var index, made unique like anndata:
  `A`, `A-1`; `duplicates` counts them), readable symbols when the index holds database ids (from
  a column such as `gene_symbols` or `feature_name`), and the matrices genes can be read from
  (`X` and layers) with their access: `direct` (CSC, or dense with narrow chunks), `memory` (a
  float32 copy built once, up to `memory.small_file_gb` and half of `memory.cache_gb`) or
  `needs_conversion`. ETag.
- `GET /api/datasets/{id}/gene?i=&matrix=X&q=0|16`: gene `i` across all cells. Sparse (uint32
  cell indices, then values) when smaller, else dense (`x-canopy-encoding`, `x-canopy-n`,
  `x-canopy-nnz`); `q=16` sends uint16 codes, value = `x-canopy-offset` + code × `x-canopy-scale`
  (65535 = missing; zeros exact; whole-number counts from 0 exact up to 65,534, larger ones sent
  as 65,534 with `x-canopy-capped`). `x-canopy-min`/`x-canopy-max` (all cells),
  `x-canopy-p99` (non-zero values) and `x-canopy-n-expressing` describe the gene. Errors:
  404 `unknown_gene` / `unknown_matrix`, 409 `needs_conversion:matrix_<layout>`, 503
  `memory_busy` (the copy would exceed `memory.budget_gb` or the host's free memory).
- Every error reply is `{"detail": {"code", "message"}, "request_id"}`; the code is also logged.
  A dataset pointing outside itself answers 409 `links_outside`; more than 24 requests waiting for
  one dataset, 503 `dataset_busy`. `GET /api/me` says which link the browser came with.
- Every request needs the access token (`server.auth`, see "On a shared Linux host"), and the
  server answers only the host names in `server.allowed_hosts` (DNS-rebinding guard; add the
  server's name and address when serving to other machines), and POST requests only as JSON.
- Files are read element by element, never loaded whole; files in the anndata < 0.8 layout are
  loaded fully when their estimated memory size is below `memory.small_file_gb`, else 409
  `needs_conversion:legacy_layout`. Open files and arrays share an LRU within `memory.cache_gb`.
- `GET /api/stores/<version>/c0/<key>`: converted stores for Viv/zarrita, with Range requests,
  streamed in 1 MB blocks. A key that resolves outside its store (via `..`, a hidden name or a
  symlink) is refused. Range replies are not cached by the browser.
- `POST /api/telemetry`: the browser's timing marks and GPU facts, logged as `client_event`.

- `GET /api/datasets/{id}/summary?kind=violin|bars|density&x=obs:<col>|gene:<i>&y=…`: what the
  Plot view draws (per-category histograms with quartiles, a count table, a 2-D histogram);
  counts and statistics only (ADR 0013).
- `GET /api/datasets/{id}/subset?n=1000000`: sorted int32 indices of representative cells for
  drawing large datasets zoomed out (a converted store's subset, else computed once, stratified
  by the first coloured categorical column).

## Conversion and jobs

- `POST /api/datasets/{id}/convert` `{"x_source": "X" | "layers/<name>"}` starts (or returns the
  running) conversion of an h5ad, AnnData-zarr or SpatialData store into a canopy store in the cache; `GET
  /api/jobs?dataset=`, `GET /api/jobs/{job}`, `POST /api/jobs/{job}/cancel` (admin link only). Once converted, the
  dataset is served from the store (every gene a direct read). Design: ADR 0009; SpatialData
  (images as zoom levels, cell outlines, transcripts by gene, one affine per coordinate system):
  ADR 0011. The spatial view needs the converted copy.
- Config `[jobs]`: `max_running` (1), `memory_gb` (2: working memory of one conversion; a job starts
  only with 1.5 × this free and is stopped beyond 2 × this + 1 GB), `tmp_dir` (spill files, about
  12 bytes per stored value; default `<cache>/tmp`), `threads` (4: threads of one conversion for
  image and mask pyramids; each holds one source block, so this bounds memory and CPU on a shared
  host).
- Each job runs in its own process and logs to `canopy-worker-<pid>.jsonl` next to the server's log.
- One installation per cache folder: the cache names its owner (`canopy-owner.json`: the state
  folder and host), and a server, `cache prune` or `catalog scan` of another installation refuses
  it. One server per state folder (`server.lock`); `catalog scan` refuses while it runs. A
  converted dataset is converted again only with `"force": true`. Free disk is checked before a job (about 20 bytes per stored value).
- Sizing: a conversion needs about 0.3 GB + 1.2–1.5 × `jobs.memory_gb` + its obs/var (about 80
  bytes per cell plus the columns). For atlases of 10⁷–10⁸ cells set `memory_gb` to 16–64.
  SpatialData conversions also hold transcripts (about 150 bytes each) and cell outlines (about
  2.6 KB each) in memory, and `threads` image or mask blocks; a job that needs more than the
  host has in total fails at once (`not_enough_memory`), see bench/README.md.
- SpatialData whose transcripts have no declared gene column and several candidates: the
  conversion fails with "Could not tell which transcript column holds the genes" and the
  details then ask for the column name (API: `{"feature_key": "<column>"}`).
- `canopy cache prune --config FILE [--dry-run]` removes converted stores of dataset versions
  the catalog no longer knows (changed, moved or removed files) and leftovers of failed
  conversions; run it with the server stopped. `--dry-run` lists every converted copy: kept (with
  the datasets using it) or to remove. It and `canopy catalog scan` need the installation's
  config (`--config` or `CANOPY_CONFIG`): without it they would look at an empty `cache` folder in
  the current directory.

## Frontend

`frontend/src/`: `api/` (typed client, TanStack Query hooks; views never call fetch), `state/`
(zustand workspace kept per browser, one saving tab), `views/` (view registry, the single deck.gl
canvas with panels per view, `colouring.tsx` shared by the cell views, `embedding/`, `spatial/`,
`plot/`, `linked.ts` and `groupMeta.ts` for linked groups, `selection.ts` for the lasso, `lod.ts`
for the level of detail), `ui/` (file browser, conversions, Radix-based controls), `colour/`
(palettes, the GPU colour extension), `telemetry/`, `styles/` (design tokens, light/dark). A new view type is one module that calls `registerView`
(see `views/registry.ts`; panels and linked zoom: ADR 0010). The spatial view reads the store
directly (`api/store.ts`: manifest, zarrita range reads; images through Viv).

## Checks

`pixi run -e dev check` runs ruff, mypy and pytest (backend), eslint, tsc and vitest (frontend)
and the browser-library read check of the reference store served by `canopy serve`.
`pixi run -e dev test-examples` runs the backend tests with the example datasets required.
`pixi run -e dev e2e` builds the frontend and runs the Playwright tests in Chromium (software
WebGL) against `canopy serve` on the example datasets; screenshots go to
`frontend/test-results/screens/`. `pixi run -e dev check-all` runs all of these.

## Environments and versions

- `pixi.toml`: dependencies with relaxed lower bounds; any pin or upper bound carries a reason.
- `pixi.lock`: exact versions and hashes for `osx-arm64` and `linux-64`. Install with `--locked`.
- `frontend/package.json` + `frontend/pnpm-lock.yaml`: the same policy for JavaScript packages.
- Environments: `default` (runtime on the host), `dev` (tests, lint, frontend toolchain), `frontend`.
- Upgrades are deliberate: `pixi update` / `pnpm update` in their own commit, after the full checks.

## Layout

```
backend/canopy/  Python package: app, CLI, catalog/, data/, inspect/, convert/, jobs/, contract/,
                 synth/, bundle, selfcheck, analyse, logs and privacy
frontend/        React + TypeScript + deck.gl 9 + Viv 0.23 + zarrita (src/, e2e/)
contract/        data contract spec and schemas
tests/backend/   backend tests (pytest)
docs/            host guide, decisions, demo guides, design notes, architecture map
bench/           benchmark results
scripts/         release, Linux trial, demo, e2e server, architecture map, git hooks
```

## Logs

Structured JSON lines, one rotating file per process: `<logging.dir>/canopy-<role>-<pid>.jsonl`
(`role` is `server`, `cli` or `worker`). Every line carries `pid`, `role` and `build`
(version + git commit, also shown by `canopy --version` and the `x-canopy-build` response header).
Requests log their route template, status and duration and return `x-request-id`; failures
return a 500 reply that carries the request id. `timed()` stages nest (`convert/x_csc/write`)
and log duration, RSS at exit, change and peak. Python warnings are logged as `python_warning`.
To collect logs from a host, run `canopy bundle --config … --since-days 2`: one archive at
strict privacy (re-filtered whatever level the runs used), with crash locations, environment,
jobs and index summaries, checked for names known on the host before it is written. `canopy token
find <h_…> --config …` (on the host) names what a token stands for. `canopy selfcheck --config …`
checks an installation in one go (config paths, folders: writable, private, file system, free
space, locks; memory against the budget; each data root's read speed; the port; the index and
unused converted copies) and logs only codes and numbers, so its results reach the next bundle.
On the developer's computer, `canopy analyse <bundle> [--previous <bundle>] [--out report.md]`
turns a bundle into a report: problems first, the performance targets, browser and server
timings, memory, conversions and the index, and the change from an earlier bundle.

**When the server seems stuck** (pages spin, nothing new in the log):
- requests running longer than `server.slow_request_s` (20 s) are logged as `slow_request` every
  30 s while they run, with route and dataset id; scans log `scan_progress` every 30 s, naming the
  file being inspected for longest;
- `canopy stacks --config …` (it signals the server named in its lock file, after checking it is
  one) writes the stack of every
  thread to `stacks-<pid>.txt` in the log folder: code locations only, no data. A hard crash
  writes its stack there too. Copy that file with the logs.
