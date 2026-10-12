"""canopy: lab single-cell and spatial data browser."""

import os
from importlib.metadata import PackageNotFoundError, version

# HDF5 file locks often fail on network shares and are not needed for reading. This covers
# libraries that open files themselves (anndata); canopy's own opens also pass locking=False.
os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

try:
    __version__ = version("canopy")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0+unknown"
