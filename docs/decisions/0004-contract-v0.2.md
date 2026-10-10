# 0004 Data contract v0.2

Date: 2026-10-10 · Status: **accepted** (owner chose option A)

## Context
The cold code review of S0–S2 (`../../../reviews/code_review_S0-S2/`) found that contract v0.1
would make spatial views slow at scale (transcripts in 1 M-row chunks, an index-only overview,
polygons not in deck.gl's fast-path form, a dense lookup for large label IDs), missed planned
fields (section key, units), and had a compatibility rule that broke on the first minor update.

## Decision
Adopt v0.2 as a whole (`contract/SPEC.md`, comparison in
`contract/history/v0.1-to-v0.2-comparison.md`). The contract code (models, validator, reference
store, writer helpers for the converter) implements all of it now; the converter uses the helpers
in S5 (tables) and S6 (spatial).

## Evidence
- `bench/contract_v02_points.py` on the MERSCOPE example (5.6 M transcripts): one median gene
  6.9 MB → 0.11 MB, zoomed-out view 39 MB → 6.7 MB; 16,384-row chunks beat 32,768 and 65,536.
- `pixi run -e dev check-store-js` (part of `check`): Viv 0.23 + zarrita 0.5.4 read every
  element of the reference store over HTTP, including sharded arrays, uint32 offsets, the overview
  and LOD groups, with assertions.

## Findings during implementation
- `@zarrita/storage` 0.1.4 default suffix read (HEAD, then range) requests `file size` bytes from
  the index start instead of the index length. Servers that clamp ranges (as HTTP requires) hide
  it; our readers use `FetchStore(url, { useSuffixRequest: true })` (one `bytes=-N` request, no
  HEAD) and the server must support suffix ranges (SPEC §1.7).
- Viv's `loadOmeZarr` probes zarr v2 metadata (`.zattrs`, `.zgroup`) before v3: two 404 round
  trips per image/labels open. Accepted; our own readers open v3 explicitly (`zarr.open.v3`).
- Polygon Morton order is defined within the bounding box of all centroids (writer and
  validator must agree on the quantisation box).

## Not yet measured
Polygon blocks/centroids at 1 M cells (C11) and deck.gl rendering speed: S6 benchmark.
