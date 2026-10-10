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

## Configuration, logs and privacy

- Every command takes `--config FILE` (or the `DBX_CONFIG` environment variable). Unknown keys,
  missing data roots and a cache inside a data root are errors, so typos never fall back silently.
- Logs go to `[logging] dir` of the config, else to `~/.cache/dbx/logs` (or `$XDG_CACHE_HOME`),
  never to the current folder.
- `dbx inspect PATH --level strict` replaces user-chosen names with tokens salted by a random
  secret created once per installation (`<cache_dir>/.dbx_salt`, or `~/.cache/dbx/.dbx_salt`
  without a config). The salt stays on the host; never copy it off with diagnostic bundles.

## Checks

`pixi run -e dev check` runs ruff, mypy and pytest (backend) and eslint, tsc and vitest (frontend).

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

Structured JSON lines in `<logging.dir>/dbx.jsonl` (rotating). Every request carries a request id
(also returned in the `x-request-id` header); `timed()` blocks log stage durations and memory.
