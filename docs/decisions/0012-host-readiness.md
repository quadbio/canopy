# 0012 Host readiness: access, files, diagnostics, memory; deferred items

Date: 2026-10-12 · Status: accepted (fix batch R7 after the full review, before the first host
test)

## Context
The full cold review (`reviews/code_review_full_1/`) found the code correct and fast but not
ready for a shared Linux host: no access control, world-readable files, no install route, logs
that bring too much and too little, memory controls that did not add up, and dropped plan items
without a record. The owner tests alone on the host and copies logs back; the developer has no
access.

## Decisions
- **Access** (`canopy.auth`): one token per installation, as in Jupyter. The server prints a
  link with it at start (`canopy link` prints it again); the link sets an HttpOnly,
  SameSite=Strict cookie and is redirected without the token. Every page and API request needs
  it (401 otherwise; `/api/health` open). `server.auth = "none"` only for a machine nobody else
  uses. Whoever has the link sees every dataset in the roots with the server user's rights: the
  docs say so. No `/docs` or `/openapi.json`. (The owner chose the token/cookie over a Unix
  socket.)
- **Files** private by default (`umask 077`; `server.file_access = "group"`: 027), in the
  server, CLI and workers; a warning when cache, state or log folders are readable by others.
- **State on a local disk**: `state_dir` holds the SQLite databases, the server lock, the salt
  and the token; `cache_dir` only the converted copies.
- **Diagnostics**: `canopy bundle` (strict re-filtering of every log line, crash files as code
  locations, environment with cgroup facts, job and index summaries, a manifest, and a
  self-check against every name known on the host); start-up failures recorded in the log
  folder; `canopy token find` on the host.
- **Memory**: one account (cache + loads + running jobs' reservations) within
  `memory.budget_gb` and the cgroup headroom (v1, hybrid and v2, limits on parent slices);
  out-of-memory kills labelled; the final store check in per-block memory; workers of a crashed
  server adopted; silent workers stopped; queue and store-size caps.
- **Install**: a release archive with the built frontend (`pixi run -e dev release`), a host
  guide (`docs/host/host-guide.md`) and a host config template.
- **Telemetry**: the browser sends error messages without names, code locations, browser, build,
  window, view and dataset; at strict the server drops message text and tokenises ids. One list
  of string fields in both (test).

## Deferred or dropped, with reasons
- **Resumable conversion stages** (plan F8): a failed or interrupted conversion starts again.
  The conversions measured so far take seconds to minutes; revisit when atlas conversions on the
  host take hours.
- **"1 large + N small" concurrent jobs** (F8): `jobs.max_running` plus the memory account cover
  it for now (small jobs pass a waiting large one).
- **Stock Vitessce opening converted stores** (SPEC promise): not checked. The store follows the
  NGFF and AnnData layouts, but only Viv and zarrita are tested (`check-store-js`). The SPEC now
  says "intended", not "opens".
- **Version hash by real path** (review A-n9): a share mounted at another path orphans
  converted copies (then pruned). Rare on one host; changing the version scheme would invalidate
  every store. Revisit if the host's mounts move.
  **Revised 2026-10-14 (owner, review H3 design note):** the version is the data root's *name*,
  the path inside the root and the stamp (`Fingerprint.identity`), so moving or remounting the
  whole data folder keeps every conversion. Moving a dataset inside the root still makes a new
  version. Upgrading: the first scan re-inspects each dataset once and renames an unchanged
  dataset's converted copy to its new version (`adopt_store`, `store_adopted`), so nothing is
  converted again; an older release cannot find those copies afterwards. Standalone
  `canopy inspect` keeps the real path.
- **Reverse-proxy sub-path** (C-m4): the app assumes it is served at `/`; with the SSH tunnel it
  is. Revisit for a stage-2 shared deployment behind the institute gateway.
- **Shapes and the transcript sort read whole** (ADR 0011): estimated honestly; streaming is the
  next step for Visium HD 2 µm bins or 10⁹ transcripts.
- **Palette slots by name hash** (B-M2): kept by the owner's decision (interpolation over
  repeats); the help text no longer promises the same colour across datasets of different
  sizes. Shared colours come with S7's workspace colour registry, together with colours picked by
  hand shared across views (S6 review B-m2).
- **View contract for plots** (B-M6): overlays for axes and non-zoomable panels are the first
  task of S7.
- **Linux**: tried 2026-10-13 with `scripts/linux-trial.sh` (Colima + Docker on the Mac): the
  release in a fresh Ubuntu 24.04 container, x86-64 (Rosetta), 4 GB memory limit, a normal user,
  through the host guide's steps; 33 checks, all passed (environment from the lock file, backend
  tests with the examples, access token, private files under umask 022, cgroup v2 limit and
  headroom, data API, hang dump, bundle, clean stop, the budget-above-limit warning). Not
  covered: the real host's limits on a parent slice, network file systems, its size.

## Revision after the second full review (2026-10-14, owner)
- **Two links.** An admin link (everything) and a member link (everything but cancelling a
  conversion; members may convert: one conversion per dataset runs at a time and everyone sees
  it). The log records `access` per request; `canopy link --member --new` revokes the member link.
- **Nothing outside a dataset is read through it** (`inspect/guard.py`): HDF5 external links,
  external storage and virtual datasets, and symlinks out of zarr stores are refused
  (`links_outside`).
- **One installation per cache** (`canopy-owner.json`); `catalog scan` refuses while a server runs.
- A stop signal without a recorded reason requeues a conversion; the guide uses `KillMode=mixed`
  and `canopy stop`.
