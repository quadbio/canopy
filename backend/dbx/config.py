"""Configuration loaded from a TOML file and validated at start-up."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

PrivacyLevel = Literal["strict", "standard", "debug"]


class DataRoot(BaseModel):
    """A folder the crawler may list. Nothing outside configured roots is ever served."""

    name: str
    path: Path

    @field_validator("path")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class LoggingConfig(BaseModel):
    dir: Path = Path("logs")
    level: Literal["debug", "info", "warning", "error"] = "info"
    # Seconds between memory samples written to the log; 0 disables sampling.
    memory_sample_interval: float = 30.0
    max_bytes: int = 50 * 1024 * 1024
    backup_count: int = 10
    privacy: PrivacyLevel = "standard"


class MemoryConfig(BaseModel):
    # Upper bound for everything the browser holds (cache + jobs), in GB.
    budget_gb: float = 8.0
    cache_gb: float = 4.0
    # Files smaller than this are loaded fully into memory in preview.
    small_file_gb: float = 2.0


class Config(BaseModel):
    data_roots: list[DataRoot] = Field(default_factory=list)
    cache_dir: Path = Path("cache")
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


def load_config(path: Path | None) -> Config:
    """Load a config file; relative paths in it are resolved against the file's folder."""
    if path is None:
        return Config()
    raw = tomllib.loads(path.read_text())
    cfg = Config.model_validate(raw)
    base = path.parent.resolve()
    if not cfg.cache_dir.is_absolute():
        cfg.cache_dir = base / cfg.cache_dir
    if not cfg.logging.dir.is_absolute():
        cfg.logging.dir = base / cfg.logging.dir
    for root in cfg.data_roots:
        root.path = (base / root.path).resolve()
    return cfg
