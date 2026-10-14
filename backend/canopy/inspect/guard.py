"""Nothing outside the dataset is read through the dataset (review full-2 A-H1).

The crawler and the catalog keep the walk inside the data roots, but a file can point elsewhere
from the inside: HDF5 external storage, external links and virtual datasets, or a symlinked chunk
or element in a zarr store. Whoever can write into a data root could then read any file the
server's account can read, through the API or a conversion. So:
- every HDF5 file is checked when it is opened (`check_h5`);
- zarr stores are opened through `GuardedStore`, which refuses a key whose real path leaves the
  store's folder;
- a SpatialData conversion (whose library opens files itself) first looks for symlinks leading
  out of the store (`links_outside`).
Such a dataset is refused with the code `links_outside`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import h5py
import zarr
from zarr.storage import LocalStore


class OutsideDataError(ValueError):
    """The dataset refers to data outside itself."""

    expected = True
    code = "links_outside"

    def __init__(self) -> None:
        super().__init__("links_outside")


def check_h5(f: h5py.File) -> None:
    """Refuse a file with external links, external storage or virtual datasets."""

    # h5py's visitors stop at the first callback returning something other than None (raising
    # inside one is not passed on cleanly)
    def link(_name: str, obj: Any) -> bool | None:
        return True if isinstance(obj, h5py.ExternalLink) else None

    def item(_name: str, obj: Any) -> bool | None:
        outside = isinstance(obj, h5py.Dataset) and (
            obj.is_virtual or obj.id.get_create_plist().get_external_count() > 0
        )
        return True if outside else None

    if f.visititems_links(link) or f.visititems(item):
        raise OutsideDataError


def _inside(real_root: str, path: str) -> bool:
    real = os.path.realpath(path)
    return real == real_root or real.startswith(real_root + os.sep)


class GuardedStore(LocalStore):
    """A read-only local zarr store that never reads past its own folder (symlinks included)."""

    def __init__(self, root: Path | str) -> None:
        super().__init__(root, read_only=True)
        self._real_root = os.path.realpath(root)

    def _check(self, key: str) -> None:
        if not _inside(self._real_root, os.path.join(self._real_root, key)):
            raise OutsideDataError

    async def get(self, key: str, prototype: Any = None, byte_range: Any = None) -> Any:
        self._check(key)
        return await super().get(key, prototype, byte_range)

    async def get_partial_values(
        self, prototype: Any, key_ranges: Iterable[tuple[str, Any]]
    ) -> list[Any]:
        key_ranges = list(key_ranges)
        for key, _ in key_ranges:
            self._check(key)
        return await super().get_partial_values(prototype, key_ranges)

    async def getsize(self, key: str) -> int:
        self._check(key)
        return await super().getsize(key)

    def get_sync(self, key: str, *, prototype: Any = None, byte_range: Any = None) -> Any:
        self._check(key)
        return super().get_sync(key, prototype=prototype, byte_range=byte_range)

    def get_ranges(self, key: str, byte_ranges: Any, **kw: Any) -> Any:  # async generator
        self._check(key)
        return super().get_ranges(key, byte_ranges, **kw)

    def get_ranges_sync(self, key: str, byte_ranges: Any, **kw: Any) -> Any:
        self._check(key)
        return super().get_ranges_sync(key, byte_ranges, **kw)


def open_zarr(path: Path | str) -> Any:
    """A zarr group of a dataset, read through `GuardedStore`."""
    return zarr.open_group(GuardedStore(path), mode="r")


def links_outside(path: Path) -> bool:
    """Whether a directory store holds a symlink whose target is outside it (looked at without
    following links: one pass over the folder, as before a conversion that reads it all)."""
    real_root = os.path.realpath(path)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p) and not _inside(real_root, p):
                return True
    return False
