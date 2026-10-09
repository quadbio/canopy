"""Environment snapshot: what the software runs on, for logs and diagnostic bundles."""

from __future__ import annotations

import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import psutil

import dbx

# Packages whose versions matter for reading and serving data.
KEY_PACKAGES = (
    "anndata",
    "h5py",
    "zarr",
    "numpy",
    "scipy",
    "pandas",
    "pyarrow",
    "spatialdata",
    "fastapi",
    "uvicorn",
    "pydantic",
    "structlog",
)


def package_versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in KEY_PACKAGES:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


def environment_snapshot() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    return {
        "dbx_version": dbx.__version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": psutil.cpu_count(logical=True),
        "memory_total_gb": round(vm.total / 2**30, 1),
        "packages": package_versions(),
    }
