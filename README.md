# dbx: lab single-cell and spatial data browser

Browse the lab data share, open h5ad / AnnData-zarr / SpatialData datasets, and view embeddings,
gene expression and spatial images side by side. Status: **stage 1 (local prototype)**.
Plan: `../reports/10_implementation_plan.md`. Progress: `PROGRESS.md`.

## Quick start (development)

```bash
pixi install --locked -e dev           # exact locked environment (Python, Node, libraries)
pixi run -e dev install-hooks          # pre-commit hook runs `check`
pixi run -e dev fe-install             # frontend packages from pnpm-lock.yaml
cp config.example.toml config.toml     # adjust data roots
pixi run -e dev build-frontend         # static frontend served by the backend
pixi run serve --config config.toml    # http://127.0.0.1:8765
```

During frontend work, `pixi run -e dev fe-dev` starts Vite on :5173 with `/api` proxied to the backend.

## Demo and architecture map

- `pixi run -e dev demo`: the current demo on this Mac at http://127.0.0.1:8765 on the example
  datasets. Walk-through and how to send feedback: `docs/demo/S4-demo-guide.md`.
- `pixi run -e dev architecture`: regenerates `docs/architecture/index.html`, a map of modules,
  classes, methods and their relationships, read from the code (open it in a browser).

## Configuration, logs and privacy

- Every command takes `--config FILE` (or the `DBX_CONFIG` environment variable). Unknown keys,
  missing data roots and a cache inside a data root are errors, so typos never fall back silently.
- Logs go to `[logging] dir` of the config, else to `~/.cache/dbx/logs` (or `$XDG_CACHE_HOME`),
  never to the current folder.
- `[logging] privacy` sets what log files may contain (they are meant to be copied off the host):
  - `standard` (default): names are kept in their own fields (`path`, `root`, `column`, …);
    free text (exception and warning messages, third-party log lines) has quoted text and
    absolute paths replaced by tokens and is cut at 300 characters. Values are never logged.
  - `strict`: every name field becomes a salted token and free text is dropped (uvicorn's
    fixed-template lines are redacted as in `standard`); exceptions keep their type and code
    locations only.
  - `debug`: everything, including full tracebacks; for the developer's own machine only.
- `dbx inspect PATH --level strict` replaces user-chosen names with tokens salted by a random
  secret created once per installation (`<cache_dir>/.dbx_salt`, or `~/.cache/dbx/.dbx_salt`
  without a config). The salt stays on the host; never copy it off with diagnostic bundles.

## Catalog

The server indexes the data roots at start-up, every `[catalog] rescan_minutes`, and on demand
(`POST /api/catalog/refresh`). `dbx catalog scan [--full] --config FILE` does one scan from the
command line. The index is `<cache_dir>/catalog.sqlite` and can be deleted at any time.

- Recognised: `.h5ad` files, AnnData-zarr and SpatialData-zarr stores. Zarr stores are never
  descended into; hidden entries and `[catalog] ignore` patterns are skipped.
- Rescans inspect only datasets whose size/modification time changed. A root that cannot be read
  (e.g. an unmounted share) keeps its entries until it is back.
- Symlinks leaving a data root are skipped; a dataset's path is checked against its root again
  every time it is opened. The API takes dataset ids and root-relative folder keys only.
- API: `GET /api/catalog/roots`, `/tree?root=&path=`, `/search?q=`, `/datasets/{id}`, `/status`;
  `POST /api/catalog/refresh?full=`.

## Dataset API and stores

- `GET /api/datasets/{id}/meta?table=`: cells, genes, obs columns (kind, categories, colours
  from `uns[<col>_colors]`), embeddings. `table` picks a SpatialData table.
- `GET /api/datasets/{id}/obs?column=` and `/obsm?key=&dims=0,1`: binary little-endian arrays
  (`x-dbx-dtype`, `x-dbx-shape`, `x-dbx-kind` headers; ETag; gzip). Category codes are int8/16/32
  with -1 for missing; numbers float32 (NaN for missing).
- Files are read element by element, never loaded whole; files in the anndata < 0.8 layout are
  loaded fully when their estimated memory size is below `memory.small_file_gb`, else 409
  `needs_conversion:legacy_layout`. Open files and arrays share an LRU within `memory.cache_gb`.
- `GET /api/stores/<version>/c0/<key>`: converted stores for Viv/zarrita, with Range requests.
- `POST /api/telemetry`: the browser's timing marks and GPU facts, logged as `client_event`.

## Frontend

`frontend/src/`: `api/` (typed client, TanStack Query hooks; views never call fetch), `state/`
(zustand workspace, kept per browser), `views/` (view registry, the single deck.gl canvas,
`embedding/`), `ui/` (file browser, Radix-based controls), `colour/` (palettes and the colour
Web Worker), `telemetry/`, `styles/` (design tokens, light/dark). A new view type is one module
that calls `registerView` (see `views/registry.ts`).

## Checks

`pixi run -e dev check` runs ruff, mypy and pytest (backend), eslint, tsc and vitest (frontend)
and the browser-library read check of the reference store served by `dbx serve`.
`pixi run -e dev test-examples` runs the backend tests with the example datasets required.
`pixi run -e dev e2e` builds the frontend and runs the Playwright tests in Chromium (software
WebGL) against `dbx serve` on the example datasets; screenshots go to
`frontend/test-results/screens/`. `pixi run -e dev check-all` runs all of these.

## Environments and versions

- `pixi.toml`: dependencies with relaxed lower bounds; any pin or upper bound carries a reason.
- `pixi.lock`: exact versions and hashes for `osx-arm64` and `linux-64`. Install with `--locked`.
- `frontend/package.json` + `frontend/pnpm-lock.yaml`: the same policy for JavaScript packages.
- Environments: `default` (runtime on the host), `dev` (tests, lint, frontend toolchain), `frontend`.
- Upgrades are deliberate: `pixi update` / `pnpm update` in their own commit, after the full checks.

## Layout

```
backend/dbx/     Python package (FastAPI app, CLI, logging, config)
frontend/        React + TypeScript + deck.gl 9 + Viv 0.23
contract/        data contract spec and schemas (S2)
tests/           backend tests
docs/decisions/  architecture decision records
bench/           benchmark results
scripts/         git hooks and helpers
```

## Logs

Structured JSON lines, one rotating file per process: `<logging.dir>/dbx-<role>-<pid>.jsonl`
(`role` is `server`, `cli` or, later, `worker`). Every line carries `pid`, `role` and `build`
(version + git commit, also shown by `dbx --version` and the `x-dbx-build` response header).
Requests log their route template, status and duration and return `x-request-id`; failures
return a 500 reply that carries the request id. `timed()` stages nest (`convert/x_csc/write`)
and log duration, RSS at exit, change and peak. Python warnings are logged as `python_warning`.
To collect logs from the host, copy the whole log folder.
