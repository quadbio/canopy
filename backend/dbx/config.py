"""Configuration loaded from a TOML file and validated at start-up."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PrivacyLevel = Literal["strict", "standard", "debug"]
PRIVACY_LEVELS: tuple[PrivacyLevel, ...] = ("strict", "standard", "debug")


class _Strict(BaseModel):
    """Unknown keys are errors: a typo in config.toml must not silently fall back to a default."""

    model_config = ConfigDict(extra="forbid")


class DataRoot(_Strict):
    """A folder the crawler may list. Nothing outside configured roots is ever served."""

    name: str
    path: Path

    @field_validator("path")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 8765


class LoggingConfig(_Strict):
    dir: Path = Path("logs")
    level: Literal["debug", "info", "warning", "error"] = "info"
    # Seconds between memory samples written to the log; 0 disables sampling.
    memory_sample_interval: float = 30.0
    # Per process file: rotated at max_bytes, keeping backup_count old files.
    max_bytes: int = 50 * 1024 * 1024
    backup_count: int = 10
    # Log files of any process untouched for this many days are deleted at start-up; 0 keeps all.
    retention_days: float = 30.0
    privacy: PrivacyLevel = "standard"


class CatalogConfig(_Strict):
    # Glob patterns matched against names and root-relative paths; matches are not indexed.
    ignore: list[str] = Field(default_factory=list)
    # Minutes between automatic rescans in the server; 0 scans only at start-up and on demand.
    rescan_minutes: float = 10.0
    # Datasets inspected in parallel during a scan.
    inspect_workers: int = Field(default=4, ge=1, le=64)
    # Count the files of zarr stores for their size (slow on network storage).
    measure_store_size: bool = False
    max_depth: int = Field(default=32, ge=1)


class MemoryConfig(_Strict):
    # Upper bound for everything the browser holds (cache + jobs), in GB.
    budget_gb: float = 8.0
    cache_gb: float = 4.0
    # Files smaller than this are loaded fully into memory in preview.
    small_file_gb: float = 2.0

    @model_validator(mode="after")
    def _cache_within_budget(self) -> MemoryConfig:
        if self.cache_gb > self.budget_gb:
            raise ValueError(f"cache_gb ({self.cache_gb}) exceeds budget_gb ({self.budget_gb})")
        return self


class Config(_Strict):
    data_roots: list[DataRoot] = Field(default_factory=list)
    cache_dir: Path = Path("cache")
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)

    @model_validator(mode="after")
    def _unique_root_names(self) -> Config:
        names = [r.name for r in self.data_roots]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate data root names: {dupes}")
        return self


class ConfigError(ValueError):
    """The configuration file is invalid; the message lists every problem."""


def check_paths(cfg: Config) -> None:
    """Checks that need the file system: roots exist, and the cache is never inside a data root
    (converted stores must not appear next to users' files) nor a root inside the cache."""
    problems = []
    cache = cfg.cache_dir.resolve()
    for root in cfg.data_roots:
        if not root.path.is_dir():
            problems.append(f"data root {root.name!r}: {root.path} is not an existing folder")
            continue
        real = root.path.resolve()
        if cache == real or cache.is_relative_to(real):
            problems.append(f"cache_dir {cache} is inside data root {root.name!r}")
        elif real.is_relative_to(cache):
            problems.append(f"data root {root.name!r} is inside cache_dir {cache}")
    if problems:
        raise ConfigError("; ".join(problems))


def load_config(path: Path | None) -> Config:
    """Load and check a config file; relative paths in it are resolved against its folder."""
    if path is None:
        return Config()
    raw = tomllib.loads(path.read_text())
    try:
        cfg = Config.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
    base = path.parent.resolve()
    if not cfg.cache_dir.is_absolute():
        cfg.cache_dir = base / cfg.cache_dir
    if not cfg.logging.dir.is_absolute():
        cfg.logging.dir = base / cfg.logging.dir
    for root in cfg.data_roots:
        root.path = (base / root.path).resolve()
    check_paths(cfg)
    return cfg
