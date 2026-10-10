# 0005 Catalog index and logging for several processes

Date: 2026-10-10 · Status: accepted (S3 + review batch 3)

## Context
S3 adds the first background work in the server (the crawler) and S5 will add worker processes
(conversion jobs). On the host, dbx is tested by the owner alone and only log files come back
(the data policy forbids direct access), so the logs must be complete, safe to copy off the host,
and correct when several processes write at once. Review findings P2, P3, P5–P8 (see
`reviews/code_review_S0-S2/SUMMARY.md`).

## Decisions

**Catalog**
- Index in SQLite at `<cache_dir>/catalog.sqlite`, one connection per operation; WAL, or the
  rollback journal when the cache is on a network file system. Rebuildable: a schema change drops
  and rebuilds it.
- Entry id = hash of (root name, root-relative path): stable across new versions of a file, so
  workspaces and links keep working; `version` (the fingerprint's dataset id) changes with it.
- Incremental rescans by version stamp (size + mtime_ns, or the store's newest group/metadata
  stamp). A failed dataset is retried only when it changes or on a `full` scan.
- An unreadable root keeps its entries (an unmounted share must not empty the catalog); a root
  removed from the config drops them.
- States now: `inspected`, `unsupported`, `failed` (reason codes only: `hdf5_lock`,
  `permission_denied`, `vanished`, `error:<Type>`). S4–S5 add preview/convert states.
- Path safety: the walk checks each symlink with `realpath`; `Catalog.resolve()` checks again at
  open time (a symlink may change after the scan). The API takes ids and folder keys only and
  answers from the index.

**Logging**
- One rotating file per process, `dbx-<role>-<pid>.jsonl`; every line has `pid`, `role`, `build`.
  Old files of any process are pruned after `retention_days`.
- Names go in dedicated fields (`NAME_FIELDS`), tokenised under `strict`. Exceptions are logged as
  type, code locations (no home/install prefix), chain types and a message redacted to the level;
  Python warnings and third-party lines are redacted the same way. Typer never prints locals.
- `timed()` nests (`a/b/c`) and reports the peak RSS seen while the stage was open (sampled every
  50 ms by one shared thread), plus the process peak.
- Requests log the route template (no ids or names), status and duration; failures return a 500
  JSON reply with the request id. Client request ids are kept only if `[A-Za-z0-9._-]{1,64}`.
- Build identity: `_build.json` in a packaged install (written in S9), else the git commit read
  from `.git`; `dbx --version`, `x-dbx-build` header, `/api/version`.
- Environment snapshot adds the HDF5 version and locking setting, available memory, and file
  system type and free space of the cache and each root (by root name, never by path).

## Evidence
- Example data (2 h5ad, 2 SpatialData) indexed in 130 ms; a 10,000-file synthetic tree with 300
  datasets indexed and rescanned in tests (rescan inspects nothing).
- A live `dbx serve` run with `privacy = "strict"`: the log file contains no user name, data
  root, dataset or folder name (checked by grep).
- Tests push secret names through fields, nested config, exception messages and chains, warnings
  and third-party log lines at all three levels.

## Known limits
- An inspection that hangs on a stalled network read blocks its scan thread (threads cannot be
  killed). The S5 job system runs work in worker processes; inspection can move there with a
  timeout if the host shows this.
- Peak RSS is per process: stages running at the same time in one process see each other's memory.
- Under `standard`, quoted text in messages becomes a token even when it is a harmless name;
  `debug` is the way to see it on a developer machine.
