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
- **Linux**: not yet run (no container tool on the development Mac). `scripts/linux-smoke.sh` is
  prepared; the host guide says the steps are untested on Linux.
