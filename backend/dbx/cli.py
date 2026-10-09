"""Command-line interface: `dbx serve`, `dbx env`, …"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from dbx.app import create_app
from dbx.config import load_config
from dbx.environment import environment_snapshot
from dbx.logs import setup_logging

app = typer.Typer(help="dbx data browser", no_args_is_help=True)

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
