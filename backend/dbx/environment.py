"""Environment snapshot: what the software runs on, for logs and diagnostic bundles."""

from __future__ import annotations

import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from dbx.build import build_info

if TYPE_CHECKING:
    from dbx.config import Config

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


def hdf5_version() -> str | None:
    try:
        import h5py

        return str(h5py.version.hdf5_version)
    except ImportError:  # pragma: no cover
        return None


def filesystem(path: Path) -> dict[str, Any]:
    """Type and free space of the file system holding `path` (no path in the result)."""
    real = os.path.realpath(path)
    best: Any = None
    try:
        for part in psutil.disk_partitions(all=True):
            mp = part.mountpoint
            if not (real == mp or real.startswith(mp.rstrip("/") + "/")):
                continue
            longer = best is None or len(mp) > len(best.mountpoint)
            # autofs often shares the mount point with the real (NFS) mount: prefer the latter.
            same_but_real = best is not None and mp == best.mountpoint and best.fstype == "autofs"
            if longer or same_but_real:
                best = part
    except OSError:
        pass
    out: dict[str, Any] = {"fstype": best.fstype if best else None}
    try:
        out["free_gb"] = round(psutil.disk_usage(real).free / 2**30, 1)
    except OSError:
        out["free_gb"] = None
    return out


NETWORK_FS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "smb3",
        "fuse.sshfs",
        "lustre",
        "gpfs",
        "beegfs",
        "ceph",
        "glusterfs",
        "fuse.glusterfs",
        "wekafs",
        "autofs",
        "9p",
    }
)
# File systems known to be local; SQLite's WAL is used only on these (it needs shared memory
# that network file systems do not provide reliably).
LOCAL_FS = frozenset(
    {
        "apfs",
        "hfs",
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "zfs",
        "tmpfs",
        "f2fs",
        "overlay",
        "ntfs",
        "jfs",
        "reiserfs",
    }
)


def environment_snapshot(cfg: Config | None = None) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    snap: dict[str, Any] = {
        "dbx_version": build_info()["version"],
        "commit": build_info()["commit"],
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": psutil.cpu_count(logical=True),
        "memory_total_gb": round(vm.total / 2**30, 1),
        "memory_available_gb": round(vm.available / 2**30, 1),
        "hdf5": hdf5_version(),
        "hdf5_file_locking": os.environ.get("HDF5_USE_FILE_LOCKING"),
        "packages": package_versions(),
    }
    if cfg is not None:
        # Roots by their configured name (`root` is a name field, tokenised in strict logs).
        snap["cache_fs"] = filesystem(cfg.cache_dir)
        snap["root_fs"] = [{"root": r.name, **filesystem(r.path)} for r in cfg.data_roots]
    return snap
