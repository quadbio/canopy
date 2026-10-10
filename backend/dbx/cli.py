"""Command-line interface: `dbx serve`, `dbx env`, …"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from dbx.app import create_app
from dbx.config import Config, ConfigError, LoggingConfig, load_config
from dbx.environment import environment_snapshot
from dbx.logs import default_log_dir, get_logger, setup_logging

app = typer.Typer(help="dbx data browser", no_args_is_help=True)
synth_app = typer.Typer(help="Generate synthetic datasets and mocks", no_args_is_help=True)
app.add_typer(synth_app, name="synth")
contract_app = typer.Typer(help="Data contract tools", no_args_is_help=True)
app.add_typer(contract_app, name="contract")

ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to config.toml", exists=True, envvar="DBX_CONFIG"),
]


class Level(StrEnum):
    standard = "standard"
    strict = "strict"


def _load(config: Path | None) -> Config:
    try:
        return load_config(config)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2) from e


@app.command()
def serve(config: ConfigOpt = None) -> None:
    """Start the web server."""
    cfg = _load(config)
    log_dir = setup_logging(cfg.logging)
    get_logger("dbx.config").info(
        "config_loaded",
        config_file=str(config) if config else None,
        log_dir=str(log_dir) if log_dir else None,
        config=cfg.model_dump(mode="json"),
    )
    # Requests are logged by our middleware, so uvicorn's access log is off.
    uvicorn.run(
        create_app(cfg),
        host=cfg.server.host,
        port=cfg.server.port,
        log_config=None,
        access_log=False,
    )


@app.command()
def env() -> None:
    """Print the environment snapshot as JSON."""
    typer.echo(json.dumps(environment_snapshot(), indent=2))


def _cli_logging(config: Path | None) -> Config | None:
    """CLI tools log to the configured log folder (or the per-user fallback, never the current
    directory) without echoing JSON events onto the terminal."""
    cfg = _load(config) if config else None
    log_cfg = cfg.logging if cfg else LoggingConfig(dir=default_log_dir())
    setup_logging(log_cfg, to_console=False)
    return cfg


def _salt(cfg: Config | None) -> str:
    from dbx.inspect.redact import installation_salt

    return installation_salt(cfg.cache_dir if cfg else default_log_dir().parent)


@app.command()
def inspect(
    path: Annotated[Path, typer.Argument(exists=True, help="h5ad file or zarr store")],
    level: Annotated[Level, typer.Option(case_sensitive=False)] = Level.standard,
    config: ConfigOpt = None,
) -> None:
    """Print a dataset's fingerprint (structure only, no values) as JSON.

    strict replaces every user-chosen name with a token salted by this installation's secret.
    """
    from dbx.inspect import inspect_path, redact

    cfg = _cli_logging(config)
    fp = inspect_path(path)
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
    from dbx.synth import write_anndata
    from dbx.synth.spec import AnnDataSpec, ColumnSpec, MatrixSpec

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
    from dbx.inspect import Fingerprint
    from dbx.synth import write_mock

    _cli_logging(config)
    fp = Fingerprint.model_validate_json(fingerprint.read_text())
    typer.echo(str(write_mock(fp, out, scale=scale, max_side=max_side, max_obs=max_obs)))


@app.command()
def validate(
    store: Annotated[Path, typer.Argument(exists=True, help="dbx store folder (…/c0)")],
    deep: Annotated[bool, typer.Option(help="Also read large arrays fully")] = False,
    config: ConfigOpt = None,
) -> None:
    """Check a dbx store against the data contract. Exit code 1 if invalid."""
    from dbx.contract import validate_store

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
    from dbx.contract.models import manifest_json_schema

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

    An existing dbx store at OUT is replaced; any other non-empty folder is refused.
    """
    from dbx.contract.example import write_example_store

    _cli_logging(config)
    try:
        typer.echo(str(write_example_store(out, force=force)))
    except FileExistsError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e
