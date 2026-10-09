"""dbx: lab single-cell and spatial data browser."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("dbx")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0+unknown"
