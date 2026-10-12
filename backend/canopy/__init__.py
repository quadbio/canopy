"""canopy: lab single-cell and spatial data browser."""

import os
from importlib.metadata import PackageNotFoundError, version

# HDF5 file locks often fail on network shares and are not needed for reading. Forced (a host
# module exporting TRUE would break conversions on NFS; the start-up log records the original
# value). Every open in the process then uses the same setting: an explicit locking flag on one
# open and the default on another of the same file fail ("locking flag values don't match").
HDF5_LOCKING_WAS = os.environ.get("HDF5_USE_FILE_LOCKING")
os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"

try:
    __version__ = version("canopy")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0+unknown"
