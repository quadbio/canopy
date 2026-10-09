"""A small uniform view over h5py and zarr groups/arrays (metadata only)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import h5py
import numpy as np
import zarr

from dbx.inspect.models import ArrayInfo


def _plain(v: Any) -> Any:
    """Convert h5py/numpy attribute values to plain Python types."""
    if isinstance(v, bytes):
        return v.decode()
    if isinstance(v, np.ndarray):
        return [_plain(x) for x in v.tolist()]
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


class Node:
    """Wraps an h5py or zarr object. Only reads metadata, never array contents."""

    def __init__(self, obj: Any) -> None:
        self.obj = obj

    @property
    def is_group(self) -> bool:
        return isinstance(self.obj, h5py.Group | zarr.Group)

    @property
    def attrs(self) -> dict[str, Any]:
        return {k: _plain(v) for k, v in dict(self.obj.attrs).items()}

    @property
    def encoding_type(self) -> str | None:
        et = self.attrs.get("encoding-type")
        return str(et) if et is not None else None

    def keys(self) -> list[str]:
        if not self.is_group:
            return []
        return sorted(self.obj.keys())

    def __contains__(self, key: str) -> bool:
        return self.is_group and key in self.obj

    def __getitem__(self, key: str) -> Node:
        return Node(self.obj[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def get(self, key: str) -> Node | None:
        if key not in self:
            return None
        return self[key]

    def items(self) -> Iterator[tuple[str, Node]]:
        for k in self.keys():
            yield k, self[k]

    @property
    def shape(self) -> list[int]:
        return [int(x) for x in self.obj.shape]

    @property
    def dtype(self) -> str:
        dt = self.obj.dtype
        try:
            return str(np.dtype(dt).name)
        except TypeError:
            return str(dt)

    @property
    def length(self) -> int:
        """Number of elements along the first axis, also for encoded groups
        (nullable arrays store `values` + `mask`, categoricals store `codes`)."""
        if not self.is_group:
            return self.shape[0] if self.shape else 0
        for key in ("values", "codes"):
            if key in self:
                return self[key].length
        return 0

    def array_info(self) -> ArrayInfo:
        return ArrayInfo(
            shape=self.shape,
            dtype=self.dtype,
            chunks=_chunks(self.obj),
            shards=_shards(self.obj),
            compression=_compression(self.obj),
        )


def _chunks(obj: Any) -> list[int] | None:
    ch = getattr(obj, "chunks", None)
    return [int(x) for x in ch] if ch else None


def _shards(obj: Any) -> list[int] | None:
    sh = getattr(obj, "shards", None)
    return [int(x) for x in sh] if sh else None


def _compression(obj: Any) -> str | None:
    if isinstance(obj, h5py.Dataset):
        return str(obj.compression) if obj.compression else None
    comps = getattr(obj, "compressors", None)
    if comps:
        names = []
        for c in comps:
            cfg = c.to_dict() if hasattr(c, "to_dict") else {}
            name = cfg.get("name") or cfg.get("id") or type(c).__name__
            inner = (cfg.get("configuration") or {}).get("cname")
            names.append(f"{name}:{inner}" if inner else str(name))
        return ",".join(names)
    return None


def attr_list(attrs: Mapping[str, Any], key: str) -> list[str]:
    v = attrs.get(key)
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]
