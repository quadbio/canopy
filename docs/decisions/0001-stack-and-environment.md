# 0001: Technology stack and environment

Date: 2026-10-09 · Status: accepted (plan approved by the user; checks done in S0)

## Decision

- **Environment:** pixi; one lockfile for `osx-arm64` (development Mac) and `linux-64` (host).
  Relaxed lower bounds; pins only with a reason. Frontend packages via pnpm with a committed lockfile.
- **Backend:** Python (resolved 3.14), FastAPI + uvicorn, structlog JSON logs, Typer CLI.
  Data: anndata 0.13, zarr 3, h5py, spatialdata 0.8 (ingest only), pyarrow.
- **Frontend:** React 19 + TypeScript + Vite; deck.gl 9.4 + luma.gl 9.4; @hms-dbmi/viv 0.23.

## Compatibility checks (S0)

| Check | Result |
|---|---|
| Viv 0.23 ↔ deck.gl | `@vivjs/layers` and `@vivjs/viewers` 0.23.0 require `@deck.gl/*` and `@luma.gl/*` **~9.4.0**. Installed: deck.gl 9.4.0, luma.gl 9.4.2; one copy of each (no duplicates) |
| zarrita | One copy, 0.5.4 (from `@vivjs/loaders`, `^0.5.4`). Our own zarr reads should use the same version to avoid a second copy |
| Viv reading zarr v3 / OME-NGFF 0.5 | Supported: `loadMultiscales` opens groups with zarrita (v2/v3 autodetect) and reads `attrs.ome` when present (NGFF ≥0.5), else top-level attrs (0.4) |
| Stock Vitessce 4.1 reading zarr v3 AnnData | Its `ZarrDataSource.getJson` maps `.zattrs` paths to zarrita `open()` of the node (v2/v3 autodetect), with zarrita 0.7.5. Expected to work; confirmed end to end in S5 when the first converted store exists |
| anndata default zarr format | anndata 0.13.4 writes zarr v3 by default (`zarr_write_format = 3`; v2 setting deprecated) |
| Rendering smoke test | deck.gl canvas with two `OrthographicView`s in one `Deck` renders in Chrome, no console errors |

## Consequence

The data contract (S2) uses **zarr v3** and **OME-NGFF 0.5** metadata (`attrs.ome`).

## Notes

- pnpm 12 blocks dependency build scripts by default; `frontend/pnpm-workspace.yaml` allows only `esbuild`.
- `httpx2` replaces `httpx` for Starlette's TestClient (httpx is deprecated there).
- The production bundle is ~950 kB minified (~280 kB gzip), mostly deck.gl; code-splitting can come later.
