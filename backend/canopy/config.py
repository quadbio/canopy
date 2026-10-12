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
    # Host names the browser may use to reach the server (the Host header, without port); "*"
    # allows any. Guards against DNS-rebinding pages. When serving to other machines, add the
    # server's name and address here.
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    # A request running longer than this is logged as `slow_request` while it runs.
    slow_request_s: float = 20.0
    # Who may use the server. "token": only browsers that opened the link with the access token
    # printed at start (kept as a cookie); "none": anyone who can reach the port (only for a
    # machine nobody else uses). Whoever is let in sees every dataset in the data roots, with the
    # rights of the user running the server.
    auth: Literal["token", "none"] = "token"
    # Files the server writes (converted copies, index, logs): "private" (only this user) or
    # "group" (also readable by the user's group, e.g. a lab sharing one cache).
    file_access: Literal["private", "group"] = "private"


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
    # Each rescan lists every folder of the roots: on a large share keep the roots narrow and
    # the interval long.
    rescan_minutes: float = 60.0
    # Datasets inspected in parallel during a scan.
    inspect_workers: int = Field(default=4, ge=1, le=64)
    # Count the files of zarr stores for their size (slow on network storage).
    measure_store_size: bool = False
    max_depth: int = Field(default=32, ge=1)
    # SQLite write-ahead log for the index: None = only when the cache is on a known local file
    # system; true/false to force.
    sqlite_wal: bool | None = None


class MemoryConfig(_Strict):
    # Upper bound for everything the browser holds (cache + jobs), in GB.
    budget_gb: float = 8.0
    cache_gb: float = 4.0
    # Legacy-layout files (anndata < 0.8) whose estimated in-memory size is below this are loaded
    # fully for preview; larger ones need conversion.
    small_file_gb: float = 2.0
    # Open datasets and loaded arrays unused for this long are dropped from the cache.
    idle_minutes: float = 30.0

    @model_validator(mode="after")
    def _cache_within_budget(self) -> MemoryConfig:
        if self.cache_gb > self.budget_gb:
            raise ValueError(f"cache_gb ({self.cache_gb}) exceeds budget_gb ({self.budget_gb})")
        return self


class JobsConfig(_Strict):
    # Conversions run one at a time by default, each in its own worker process.
    max_running: int = Field(default=1, ge=1, le=16)
    # Working memory of one conversion (the converter sizes its blocks to it), in GB. A job
    # starts only when the host has 1.5 times this available; it is stopped if its process
    # grows beyond twice this (plus 1 GB for the interpreter and libraries).
    memory_gb: float = Field(default=2.0, gt=0)
    # Threads of one conversion for block-wise work (image and mask pyramids). Each thread holds
    # one source block, so this bounds memory and CPU use on a shared host.
    threads: int = Field(default=4, ge=1, le=64)
    # Temporary files of conversions (about the size of the expression matrix); default
    # <cache_dir>/tmp.
    tmp_dir: Path | None = None


class Config(_Strict):
    data_roots: list[DataRoot] = Field(default_factory=list)
    # Converted copies (large; may be on shared storage).
    cache_dir: Path = Path("cache")
    # Small state: the index and job databases, the server lock, the privacy salt and the access
    # token. SQLite and file locks are unreliable on network storage: put this on a local disk.
    # Default: the cache folder.
    state_dir: Path | None = None
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)

    @property
    def state(self) -> Path:
        return self.state_dir or self.cache_dir

    @model_validator(mode="after")
    def _unique_root_names(self) -> Config:
        names = [r.name for r in self.data_roots]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate data root names: {dupes}")
        return self


class ConfigError(ValueError):
    """The configuration file is invalid; the message lists every problem. `code` names the
    kind (config_syntax, config_invalid, config_paths) for the start-up failure record."""

    def __init__(self, message: str, code: str = "config_invalid", **facts: int) -> None:
        super().__init__(message)
        self.code = code
        self.facts = facts


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
        raise ConfigError("; ".join(problems), "config_paths", n_problems=len(problems))


def load_config(path: Path | None) -> Config:
    """Load and check a config file; relative paths in it are resolved against its folder."""
    if path is None:
        return Config()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        line, col = getattr(e, "lineno", 0), getattr(e, "colno", 0)
        raise ConfigError(
            f"{path}: not valid TOML at line {line}, column {col}: {e.msg}",  # type: ignore[attr-defined]
            "config_syntax",
            line=line,
            column=col,
        ) from e
    try:
        cfg = Config.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
    base = path.parent.resolve()
    if not cfg.cache_dir.is_absolute():
        cfg.cache_dir = base / cfg.cache_dir
    if not cfg.logging.dir.is_absolute():
        cfg.logging.dir = base / cfg.logging.dir
    if cfg.state_dir is not None and not cfg.state_dir.is_absolute():
        cfg.state_dir = base / cfg.state_dir
    for root in cfg.data_roots:
        root.path = (base / root.path).resolve()
    check_paths(cfg)
    return cfg


def apply_file_access(cfg: Config) -> None:
    """Files this process creates: private to the user, or also readable by the group."""
    import os

    os.umask(0o077 if cfg.server.file_access == "private" else 0o027)
