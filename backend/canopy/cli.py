"""Command-line interface: `canopy serve`, `canopy env`, …"""

from __future__ import annotations

import json
import os
import sys
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from canopy.app import create_app
from canopy.auth import access_token, link
from canopy.build import build_id, build_info
from canopy.config import Config, ConfigError, LoggingConfig, apply_file_access, load_config
from canopy.environment import environment_snapshot
from canopy.logs import default_log_dir, get_logger, install_stack_dumps, setup_logging
from canopy.privacy import installation_salt


def _typer(help: str) -> typer.Typer:
    # Tracebacks on the terminal never show local variables (they can hold data values).
    return typer.Typer(help=help, no_args_is_help=True, pretty_exceptions_show_locals=False)


app = _typer("Canopy: single-cell and spatial data browser")
synth_app = _typer("Generate synthetic datasets and mocks")
app.add_typer(synth_app, name="synth")
contract_app = _typer("Data contract tools")
app.add_typer(contract_app, name="contract")
catalog_app = _typer("Index the data roots")
app.add_typer(catalog_app, name="catalog")

ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to config.toml", exists=True, envvar="CANOPY_CONFIG"),
]


class Level(StrEnum):
    standard = "standard"
    strict = "strict"


def _version(value: bool) -> None:
    if value:
        typer.echo(f"canopy {build_id()}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version and build"),
    ] = False,
) -> None:
    """Canopy: single-cell and spatial data browser."""
    os.umask(0o077)  # private until a config says otherwise


def _configured_log_dir(config: Path) -> Path | None:
    """The `[logging] dir` of a config file, also when the file is not valid TOML (line scan)."""
    import re
    import tomllib

    try:
        text = config.read_text()
    except OSError:
        return None
    raw: str | None
    try:
        raw = tomllib.loads(text).get("logging", {}).get("dir")
    except ValueError:
        raw, section = None, ""
        for line in text.splitlines():
            head = re.match(r"\s*\[([^\]]+)\]", line)
            if head:
                section = head.group(1).strip()
            m = re.match(r'\s*dir\s*=\s*"([^"]+)"', line)
            if m and section == "logging":
                raw = m.group(1)
                break
    if not raw:
        return None
    return Path(raw) if Path(raw).is_absolute() else config.parent / raw


def _startup_failure(code: str, config: Path | None, cfg: Config | None, **facts: object) -> None:
    """A start-up failure leaves a record in the log folder (the configured one, else the
    per-user fallback), so it reaches the developer with the copied logs, not only the terminal.
    Codes and numbers only: no paths or messages."""
    import datetime

    log_dir = cfg.logging.dir if cfg else None
    if log_dir is None and config is not None:
        log_dir = _configured_log_dir(config)
    for d in (log_dir, default_log_dir()):
        if d is None:
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            line = {
                "event": "startup_failed",
                "code": code,
                **facts,
                "build": build_id(),
                "pid": os.getpid(),
                "role": "server",
                "level": "error",
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            }
            with (d / f"canopy-startup-{os.getpid()}.jsonl").open("a") as f:
                f.write(json.dumps(line) + "\n")
            return
        except OSError:
            continue


def _load(config: Path | None, *, serving: bool = False) -> Config:
    try:
        cfg = load_config(config)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        if serving:
            _startup_failure(e.code, config, None, **e.facts)
        raise typer.Exit(2) from e
    apply_file_access(cfg)
    return cfg


@app.command()
def serve(config: ConfigOpt = None) -> None:
    """Start the web server."""
    cfg = _load(config, serving=True)
    port_problem = _port_free(cfg.server.host, cfg.server.port)
    if port_problem is not None:
        typer.echo(
            f"port {cfg.server.port} cannot be used (errno {port_problem}): another program "
            "uses it, or choose server.port",
            err=True,
        )
        _startup_failure("port_in_use", config, cfg, port=cfg.server.port, errno=port_problem)
        raise typer.Exit(1)
    log_dir = setup_logging(cfg.logging, role="server", salt=_salt(cfg))
    stacks = install_stack_dumps(log_dir)
    get_logger("canopy.config").info(
        "config_loaded",
        config_file=str(config) if config else None,
        log_dir=str(log_dir) if log_dir else None,
        stack_dump_file=stacks.name if stacks else None,
        config=cfg.model_dump(mode="json"),
        build=build_info(),
    )
    from canopy.jobs.manager import CacheInUseError, CacheOwnedError, LockUnsupportedError

    try:
        server = create_app(cfg)
    except CacheOwnedError as e:
        typer.echo(str(e), err=True)
        get_logger("canopy.app").error("startup_failed", code="cache_owned")
        _startup_failure("cache_owned", config, cfg)
        raise typer.Exit(1) from e
    except CacheInUseError as e:
        typer.echo(str(e), err=True)
        get_logger("canopy.app").error("startup_failed", code="cache_locked")
        _startup_failure("cache_locked", config, cfg)
        raise typer.Exit(1) from e
    except LockUnsupportedError as e:
        typer.echo(str(e), err=True)
        errno_ = getattr(e.__cause__, "errno", None)
        get_logger("canopy.app").error("startup_failed", code="lock_unsupported", errno=errno_)
        _startup_failure("lock_unsupported", config, cfg, errno=errno_)
        raise typer.Exit(1) from e
    if cfg.server.auth == "token":
        url = link(cfg.server.host, cfg.server.port, access_token(cfg.state))
        if sys.stderr.isatty():  # a terminal (also tmux, screen): only its user sees it
            typer.echo(
                f"\nCanopy: open {url}\n(Keep this admin link private: it opens every dataset. "
                "For lab members: canopy link --member --config <config>)\n",
                err=True,
            )
        else:  # output going to a file or journal others may read: no token in it
            typer.echo(
                "\nCanopy: started; print the access link with: canopy link --config <config>\n",
                err=True,
            )
    else:
        typer.echo(
            'Canopy: server.auth = "none": anyone who reaches the port sees all data.', err=True
        )
    # Requests are logged by our middleware, so uvicorn's access log is off.
    uvicorn.run(
        server,
        host=cfg.server.host,
        port=cfg.server.port,
        log_config=None,
        access_log=False,
    )


token_app = _typer("Strict-privacy tokens in logs")
app.add_typer(token_app, name="token")


def _strings(value: object, out: set[str]) -> None:
    """Every string (and dict key) in a JSON value: the names a fingerprint holds."""
    if isinstance(value, str):
        if 0 < len(value) <= 300:
            out.add(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            _strings(k, out)
            _strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _strings(v, out)


@token_app.command("find")
def token_find(
    tokens: Annotated[
        list[str], typer.Argument(help="Tokens from a strict log, e.g. h_fb26d42600")
    ],
    config: ConfigOpt = None,
) -> None:
    """Which name a strict-log token stands for. Run it on the host only: it reads the index
    and prints real names (to the terminal, never to a log)."""
    from canopy.catalog.db import DB_FILE, CatalogDB
    from canopy.privacy import token

    cfg = _load(_required(config))
    salt = _salt(cfg)
    candidates: dict[str, str] = {}

    def add(kind: str, value: object) -> None:
        if isinstance(value, str) and value:
            candidates.setdefault(token(value, salt), f"{kind}: {value}")

    for r in cfg.data_roots:
        add("data root", r.name)
        add("data root path", str(r.path))
    for kind, v in (
        ("cache folder", cfg.cache_dir),
        ("state folder", cfg.state),
        ("log folder", cfg.logging.dir),
        ("job tmp folder", cfg.jobs.tmp_dir),
        ("config file", config),
    ):
        if v is not None:
            add(kind, str(v))
    for h in cfg.server.allowed_hosts:
        add("host name", h)
    for g in cfg.catalog.ignore:
        add("ignore pattern", g)
    db = CatalogDB(cfg.state / DB_FILE)
    with db.connect() as con:
        rows = con.execute(
            "SELECT id, root, relpath, version, fingerprint FROM datasets"
        ).fetchall()
    for row in rows:
        id_, root, rel, version, fp = row[0], row[1], row[2], row[3], row[4]
        add(f"dataset id ({root}/{rel})", id_)
        add("dataset path", rel)
        add("dataset path", f"{root}/{rel}")
        for part in Path(rel).parts:
            add(f"file or folder name in {root}", part)
        if version:
            add(f"dataset version ({root}/{rel})", version)
        names: set[str] = set()
        if fp:
            _strings(json.loads(fp), names)
        for n in names:
            add(f"name in {root}/{rel}", n)
    for t in tokens:
        typer.echo(
            f"{t}  →  {candidates.get(t, 'not found (a gene name, or a name not in the index)')}"
        )


def _port_free(host: str, port: int) -> int | None:
    """None if the server can listen on host:port, else the errno."""
    from canopy.selfcheck import port_free

    return port_free(host, port)


@app.command()
def bundle(
    config: ConfigOpt = None,
    since_days: Annotated[float, typer.Option("--since-days", help="Logs of the last N days")] = 3,
    out: Annotated[Path | None, typer.Option("--out", help="Archive to write")] = None,
    show_names: Annotated[
        bool,
        typer.Option("--show-names", help="If refused, print the names found (to you only)"),
    ] = False,
    accept_name: Annotated[
        list[str] | None,
        typer.Option(
            "--accept-name",
            help="A name found by the check that may stay (repeatable): only after reading "
            "where it appears (--show-names) and judging it harmless",
        ),
    ] = None,
) -> None:
    """Collect logs, environment, jobs and index summaries for the developer, at strict privacy,
    into one archive (checked for names known on this host before it is written)."""
    from canopy.bundle import BundleRefusedError, build_bundle

    cfg = _load(_required(config))
    stamp = time.strftime("%Y%m%d-%H%M")
    target = out or Path.cwd() / f"canopy-bundle-{stamp}.tar.gz"
    try:
        path, notes = build_bundle(cfg, target, since_days, frozenset(accept_name or ()))
    except BundleRefusedError as e:
        typer.echo(f"not written: {e}", err=True)
        if show_names:
            for f, name in sorted(e.hits):
                typer.echo(f"  {f}: {name}", err=True)
            typer.echo(
                "These are for you: do not send them. Tell the developer which kind of value "
                "matched (e.g. a version number, a column name) and in which file. If a match "
                "is harmless (a common word that is also one of your names), --accept-name "
                "NAME lets it through.",
                err=True,
            )
        else:
            typer.echo("To see the names found (on this terminal only): add --show-names", err=True)
        raise typer.Exit(1) from e
    for n in notes:
        typer.echo(f"note: {n}")
    typer.echo(
        f"wrote {path} ({path.stat().st_size // 1024} KB). Open it and read MANIFEST.md before"
    )
    typer.echo("copying it; it holds no names (checked), only codes, numbers and tokens.")


@app.command()
def selfcheck(
    config: ConfigOpt = None,
    read_mb: Annotated[
        int, typer.Option("--read-mb", help="MB to read from each data root to time it (0: skip)")
    ] = 256,
) -> None:
    """Check this installation: config paths, folders (writable, private, file system, free
    space, locks), memory against the budget, data roots (readable, read speed), the port, the
    index and unused converted copies. Exit code 1 if a check failed. Logs only codes and
    numbers, so the results reach the next bundle."""
    from canopy.selfcheck import run

    path = _required(config)
    cfg = _cli_logging(path)
    assert cfg is not None
    checks = run(cfg, path, read_mb)
    mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    for c in checks:
        typer.echo(f"{mark[c.status]}  {c.message}")
    n = {s: sum(c.status == s for c in checks) for s in ("ok", "warn", "fail")}
    typer.echo(f"\n{n['ok']} ok, {n['warn']} warnings, {n['fail']} failed")
    if n["fail"]:
        raise typer.Exit(1)


@app.command()
def analyse(
    bundle: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="A bundle (.tar.gz)")],
    previous: Annotated[
        Path | None,
        typer.Option(
            "--previous", exists=True, dir_okay=False, help="An earlier bundle to compare"
        ),
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Write the report here")] = None,
) -> None:
    """Report on a diagnostics bundle (on the developer's computer): problems, timings against
    the performance targets, conversions, index, memory, browser measurements."""
    from canopy.analyse import analyse as run

    text = run(bundle, previous)
    if out is None:
        typer.echo(text)
    else:
        out.write_text(text)
        typer.echo(f"wrote {out}")


def _server_pid(cfg: Config) -> int:
    """The running server's process id, from its lock file, only if that process really is a
    Canopy server (after a crash or a prune the file may name another process; full-2 A-L9)."""
    import psutil

    try:
        pid = int((cfg.state / "server.lock").read_text().strip())
        proc = psutil.Process(pid)
        cmd = " ".join(proc.cmdline())
    except (OSError, ValueError, psutil.Error):
        pid, cmd = 0, ""
    if not pid or "canopy" not in cmd or " serve" not in cmd:
        typer.echo("no Canopy server is running for this config", err=True)
        raise typer.Exit(1)
    return pid


@app.command()
def stop(config: ConfigOpt = None) -> None:
    """Stop the server of this config (running conversions run again at its next start)."""
    import signal

    pid = _server_pid(_load(_required(config)))
    os.kill(pid, signal.SIGTERM)
    typer.echo(f"asked the server (pid {pid}) to stop")


@app.command()
def stacks(config: ConfigOpt = None) -> None:
    """Have the server write the stacks of all its threads to stacks-<pid>.txt in its log folder
    (code locations only), for a page that hangs."""
    import signal

    pid = _server_pid(_load(_required(config)))
    os.kill(pid, signal.SIGUSR1)
    typer.echo(f"asked the server (pid {pid}) for its stacks: stacks-{pid}.txt in the log folder")


@app.command("link")
def link_cmd(
    config: ConfigOpt = None,
    member: Annotated[
        bool,
        typer.Option("--member", help="The member link (everything but cancelling conversions)"),
    ] = False,
    new: Annotated[
        bool,
        typer.Option(
            "--new",
            help="Replace this link's token first: the old link stops working (within seconds)",
        ),
    ] = False,
) -> None:
    """Print an access link of this installation: the admin link (everything), or with --member
    the link for lab members. --new revokes the old one (e.g. when someone leaves the lab)."""
    from canopy.auth import member_token, rotate

    cfg = _load(_required(config))
    if cfg.server.auth != "token":
        typer.echo('server.auth is "none": no token needed.')
        return
    which = "member" if member else "admin"
    if new:
        rotate(cfg.state, which)
        typer.echo(f"new {which} link (the old one no longer opens Canopy):", err=True)
    token = member_token(cfg.state) if member else access_token(cfg.state)
    typer.echo(link(cfg.server.host, cfg.server.port, token))


@app.command()
def env() -> None:
    """Print the environment snapshot as JSON."""
    typer.echo(json.dumps(environment_snapshot(), indent=2))


def _cli_logging(config: Path | None) -> Config | None:
    """CLI tools log to the configured log folder (or the per-user fallback, never the current
    directory) without echoing JSON events onto the terminal."""
    cfg = _load(config) if config else None
    log_cfg = cfg.logging if cfg else LoggingConfig(dir=default_log_dir())
    setup_logging(log_cfg, role="cli", salt=_salt(cfg), to_console=False)
    return cfg


def _required(config: Path | None) -> Path:
    """Commands that act on an installation's cache and index need its config: without one, the
    defaults would point at a `cache` folder in the current directory (an empty one, made on the
    spot), and the command would report on that instead."""
    if config is None:
        typer.echo(
            "error: which installation? Give its config: --config config.toml (or set "
            "CANOPY_CONFIG)",
            err=True,
        )
        raise typer.Exit(2)
    return config


def _own_cache(cfg: Config) -> None:
    """The server's lock (none may run: review full-2 A-M2) and the cache's owner (it must be
    this installation's: A-M1), or exit with the reason."""
    from canopy.jobs.manager import (
        CacheInUseError,
        CacheOwnedError,
        LockUnsupportedError,
        claim_cache,
        lock_cache,
    )

    try:
        lock_cache(cfg.state)
        claim_cache(cfg.cache_dir, cfg.state)
    except (CacheInUseError, CacheOwnedError, LockUnsupportedError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


def _salt(cfg: Config | None) -> str:
    return installation_salt(cfg.state if cfg else default_log_dir().parent)


@app.command()
def inspect(
    path: Annotated[Path, typer.Argument(exists=True, help="h5ad file or zarr store")],
    level: Annotated[Level, typer.Option(case_sensitive=False)] = Level.standard,
    config: ConfigOpt = None,
) -> None:
    """Print a dataset's fingerprint (structure only, no values) as JSON.

    strict replaces every user-chosen name with a token salted by this installation's secret.
    """
    from canopy.inspect import inspect_path, redact

    cfg = _cli_logging(config)
    fp = inspect_path(path)
    if level is Level.strict and cfg is None:
        typer.echo(
            "note: without --config the tokens use this user's own salt; they will not match "
            "the server's logs (pass the server's --config)",
            err=True,
        )
    fp = redact(fp, level.value, _salt(cfg) if level is Level.strict else "")
    typer.echo(fp.model_dump_json(indent=2))


@synth_app.command("anndata")
def synth_anndata(
    out: Annotated[Path, typer.Argument(help="Output .h5ad or .zarr")],
    n_obs: Annotated[int, typer.Option()] = 100_000,
    n_vars: Annotated[int, typer.Option()] = 2_000,
    density: Annotated[float, typer.Option()] = 0.05,
    encoding: Annotated[str, typer.Option(help="csr | csc | dense")] = "csr",
    dtype: Annotated[str, typer.Option()] = "float32",
    chunk: Annotated[int | None, typer.Option(help="1-D chunk length of X data")] = None,
    compression: Annotated[str, typer.Option(help="gzip | lzf | zstd | none")] = "gzip",
    n_clusters: Annotated[int, typer.Option()] = 30,
    config: ConfigOpt = None,
) -> None:
    """Write a synthetic AnnData file (X is streamed, so it may exceed RAM)."""
    from canopy.synth import write_anndata
    from canopy.synth.spec import AnnDataSpec, ColumnSpec, MatrixSpec

    _cli_logging(config)
    spec = AnnDataSpec(
        n_obs=n_obs,
        n_vars=n_vars,
        X=MatrixSpec.model_validate(
            {
                "encoding": encoding,
                "dtype": dtype,
                "density": density,
                "chunk": chunk,
                "compression": compression,
            }
        ),
        obs=[
            ColumnSpec(name="cluster", kind="categorical", n_categories=n_clusters, colors=True),
            ColumnSpec(name="total_counts", kind="numeric"),
        ],
        obsm={"X_umap": 2, "X_pca": 50},
    )
    typer.echo(str(write_anndata(spec, out)))


@synth_app.command("demo")
def synth_demo(
    out: Annotated[Path, typer.Argument(help="Output .h5ad")],
    n_obs: Annotated[int, typer.Option()] = 8_000,
    config: ConfigOpt = None,
) -> None:
    """Write the small example with layers (counts + normalised X, marker genes)."""
    from canopy.synth import write_demo

    _cli_logging(config)
    typer.echo(str(write_demo(out, n_obs=n_obs)))


@synth_app.command("mock")
def synth_mock(
    fingerprint: Annotated[Path, typer.Argument(exists=True, help="Fingerprint JSON")],
    out: Annotated[Path, typer.Argument(help="Output path (.h5ad or .zarr)")],
    scale: Annotated[float, typer.Option(help="Fraction of cells/rows to generate")] = 0.1,
    max_side: Annotated[int, typer.Option(help="Largest image side in pixels")] = 1024,
    max_obs: Annotated[int, typer.Option()] = 200_000,
    config: ConfigOpt = None,
) -> None:
    """Rebuild a look-alike dataset from a fingerprint (random values, same structure)."""
    from canopy.inspect import Fingerprint
    from canopy.synth import write_mock

    _cli_logging(config)
    fp = Fingerprint.model_validate_json(fingerprint.read_text())
    typer.echo(str(write_mock(fp, out, scale=scale, max_side=max_side, max_obs=max_obs)))


@app.command()
def validate(
    store: Annotated[Path, typer.Argument(exists=True, help="canopy store folder (…/c0)")],
    deep: Annotated[bool, typer.Option(help="Also read large arrays fully")] = False,
    config: ConfigOpt = None,
) -> None:
    """Check a canopy store against the data contract. Exit code 1 if invalid."""
    from canopy.contract import validate_store

    _cli_logging(config)
    rep = validate_store(store, deep=deep)
    for e in rep.errors:
        typer.echo(f"ERROR   {e}")
    for w in rep.warnings:
        typer.echo(f"WARNING {w}")
    typer.echo("valid" if rep.ok else f"invalid ({len(rep.errors)} errors)")
    raise typer.Exit(0 if rep.ok else 1)


@contract_app.command("schema")
def contract_schema(
    out: Annotated[Path | None, typer.Option(help="Write to file instead of stdout")] = None,
) -> None:
    """Print (or write) the manifest JSON schema."""
    from canopy.contract.models import manifest_json_schema

    text = json.dumps(manifest_json_schema(), indent=2) + "\n"
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text)


@contract_app.command("example")
def contract_example(
    out: Annotated[Path, typer.Argument(help="Output store folder, e.g. .cache/example/c0")],
    force: Annotated[
        bool, typer.Option(help="Replace OUT even if it is a non-empty folder that is not a store")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Write the small synthetic reference store that shows every contract element.

    An existing canopy store at OUT is replaced; any other non-empty folder is refused.
    """
    from canopy.contract.example import write_example_store

    _cli_logging(config)
    try:
        typer.echo(str(write_example_store(out, force=force)))
    except FileExistsError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e


@catalog_app.command("scan")
def catalog_scan(
    full: Annotated[bool, typer.Option(help="Inspect every dataset again")] = False,
    config: ConfigOpt = None,
) -> None:
    """Index the configured data roots once (what the server does in the background). Refuses
    while a server runs: two scans of one index can delete each other's rows (press Refresh in
    the browser instead)."""
    from canopy.catalog import CatalogDB, scan
    from canopy.catalog.db import DB_FILE

    cfg = _cli_logging(_required(config))
    assert cfg is not None
    _own_cache(cfg)
    db = CatalogDB(cfg.state / DB_FILE)
    counts = scan(
        db,
        cfg.data_roots,
        cfg.catalog,
        full=full,
        trigger="cli",
        cache_dir=cfg.cache_dir,
        state_dir=cfg.state,
    )
    typer.echo(json.dumps(counts.as_dict(), indent=2))


cache_app = _typer("The cache folder: converted stores and indexes.")
app.add_typer(cache_app, name="cache")


@cache_app.command("prune")
def cache_prune(
    config: ConfigOpt = None,
    dry_run: Annotated[
        bool, typer.Option(help="Only list the converted copies: kept (and why) or removed")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Remove even when that is more than half of the copies"),
    ] = False,
) -> None:
    """Remove converted stores of dataset versions the catalog no longer knows (old versions of
    changed, moved or removed files) and leftovers of failed conversions. Refuses while a server
    uses the cache, without an index that has finished a scan, and (without --yes) when more
    than half of the copies would go."""
    import shutil

    from canopy.catalog import Catalog
    from canopy.catalog.db import DB_FILE
    from canopy.jobs.manager import tmp_dirs

    # checked before logging starts: that writes the installation's salt into the state folder
    # (by default the cache folder), which would make the folder this checks for
    pre = _load(_required(config))
    if not pre.cache_dir.is_dir():
        typer.echo(f"error: the cache folder {pre.cache_dir} does not exist", err=True)
        raise typer.Exit(1)
    # prune trusts the index: a missing or new one knows no versions and would remove every
    # copy (review H3 A-H1: a state folder absent on this machine, a deleted index)
    index = pre.state / DB_FILE
    if not index.is_file():
        typer.echo(
            f"error: no index at {index}: prune removes the copies the index does not know, so "
            "it needs the server's index (is state_dir right, and on this machine?)",
            err=True,
        )
        raise typer.Exit(1)
    cfg = _cli_logging(config)
    assert cfg is not None
    _own_cache(cfg)
    db = Catalog(cfg).db
    with db.connect() as con:
        scanned = con.execute("SELECT COUNT(*) FROM scans WHERE status='done'").fetchone()[0]
    users = db.version_users()
    typer.echo(f"cache folder {cfg.cache_dir}; index {index}")
    typer.echo(f"{len(users)} dataset versions in the index")
    if not scanned or not users:
        typer.echo(
            "error: the index has no finished scan or no datasets: start the server (or run "
            "`canopy catalog scan --config …`) and let it index the data roots first",
            err=True,
        )
        raise typer.Exit(1)

    stores = [
        d
        for d in sorted(p for p in cfg.cache_dir.iterdir() if p.is_dir())
        if any(d.glob("c*/manifest.json")) or any(d.glob("c*.tmp-*"))
    ]
    unused = [d for d in stores if d.name not in users]
    if not dry_run and not yes and len(unused) * 2 > len(stores):
        typer.echo(
            f"error: {len(unused)} of {len(stores)} converted copies would be removed. If that is "
            "right (see the list with --dry-run), run again with --yes",
            err=True,
        )
        raise typer.Exit(1)

    def size(d: Path) -> int:
        return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())

    log = get_logger("canopy.cache")
    removed = kept = 0
    for d in stores:
        if d.name in users:
            if dry_run:  # names on the owner's terminal only, never in a log; kept copies are
                n = size(d)  # measured only here (file by file: slow on network storage)
                kept += n
                typer.echo(f"keep         {d.name} {n / 2**30:7.2f} GB  {', '.join(users[d.name])}")
            continue
        n = size(d)
        typer.echo(
            f"{'would remove' if dry_run else 'removing'} {d.name} {n / 2**30:7.2f} GB  "
            "(no dataset in the index at this version)"
        )
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)
            log.info("store_pruned", version=d.name, bytes=n)
        removed += n
    for t in tmp_dirs(cfg.cache_dir, cfg.jobs.tmp_dir):
        typer.echo(f"{'would remove' if dry_run else 'removing'} leftover {t.name}")
        if not dry_run:
            shutil.rmtree(t, ignore_errors=True)
    n_kept = len(stores) - len(unused)
    typer.echo(f"{n_kept} converted copies kept" + (f" ({kept / 2**30:.2f} GB)" if dry_run else ""))
    typer.echo(
        f"{len(unused)} {'to remove' if dry_run else 'removed'}: "
        f"{'would free' if dry_run else 'freed'} {removed / 2**30:.2f} GB"
    )
