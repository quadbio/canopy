"""Dataset inspection: structure only, never values."""

from dbx.inspect.core import dataset_id, detect_kind, inspect_path, is_zarr_store
from dbx.inspect.models import FINGERPRINT_VERSION, Fingerprint
from dbx.inspect.redact import redact

__all__ = [
    "FINGERPRINT_VERSION",
    "Fingerprint",
    "dataset_id",
    "detect_kind",
    "inspect_path",
    "is_zarr_store",
    "redact",
]
