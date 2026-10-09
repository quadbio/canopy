"""Command-line interface: `dbx serve`, `dbx env`, …"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from dbx.app import create_app
from dbx.config import LoggingConfig, load_config
from dbx.environment import environment_snapshot
from dbx.logs import setup_logging

app = typer.Typer(help="dbx data browser", no_args_is_help=True)
synth_app = typer.Typer(help="Generate synthetic datasets and mocks", no_args_is_help=True)
app.add_typer(synth_app, name="synth")

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to config.toml", exists=True)
]


@app.command()
def serve(config: ConfigOpt = None) -> None:
    """Start the web server."""
    cfg = load_config(config)
    setup_logging(cfg.logging)
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


def _quiet_logging() -> None:
    """CLI tools log to ./logs without echoing JSON events onto the terminal."""
    setup_logging(LoggingConfig(level="warning"), to_console=False)


@app.command()
def inspect(
    path: Annotated[Path, typer.Argument(exists=True, help="h5ad file or zarr store")],
    level: Annotated[str, typer.Option(help="standard | strict")] = "standard",
    salt: Annotated[str, typer.Option(help="Salt for strict-mode name hashing")] = "dbx",
) -> None:
    """Print a dataset's fingerprint (structure only, no values) as JSON."""
    from dbx.inspect import inspect_path, redact

    _quiet_logging()
    fp = redact(inspect_path(path), level, salt)
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
) -> None:
    """Write a synthetic AnnData file (X is streamed, so it may exceed RAM)."""
    from dbx.synth import write_anndata
    from dbx.synth.spec import AnnDataSpec, ColumnSpec, MatrixSpec

    _quiet_logging()
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
) -> None:
    """Rebuild a look-alike dataset from a fingerprint (random values, same structure)."""
    from dbx.inspect import Fingerprint
    from dbx.synth import write_mock

    _quiet_logging()
    fp = Fingerprint.model_validate_json(fingerprint.read_text())
    typer.echo(str(write_mock(fp, out, scale=scale, max_side=max_side, max_obs=max_obs)))
