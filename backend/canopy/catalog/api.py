"""Catalog HTTP API. Answers come from the index only: no client-supplied path ever reaches the
file system (folders are matched as index keys, datasets are addressed by id)."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from canopy.catalog.db import Row
from canopy.catalog.service import Catalog
from canopy.errors import ApiError

router = APIRouter(prefix="/api/catalog", tags=["catalog"])

MAX_PATH = 4096


class DatasetEntry(BaseModel):
    id: str
    root: str
    path: str  # relative to the root, '/'-separated
    name: str
    kind: str | None
    state: str  # discovered | inspected | unsupported | failed (later: converting, converted)
    reason: str | None
    size_bytes: int | None
    mtime: float | None
    version: str | None
    summary: dict[str, Any]


class DatasetDetail(DatasetEntry):
    # The fingerprint, with `path` relative to the root (host paths are not sent to browsers).
    fingerprint: dict[str, Any] | None
    # Converted or not, and the latest conversion job of this version (see canopy.jobs.api).
    conversion: dict[str, Any] | None = None


class FolderEntry(BaseModel):
    name: str
    path: str
    n_datasets: int


class FolderListing(BaseModel):
    root: str
    path: str
    folders: list[FolderEntry]
    datasets: list[DatasetEntry]


class RootEntry(BaseModel):
    name: str
    n_datasets: int
    states: dict[str, int]


class CatalogStatus(BaseModel):
    scanning: bool
    last_scan: dict[str, Any] | None


def _catalog(request: Request) -> Catalog:
    cat: Catalog = request.app.state.catalog
    return cat


def _entry(r: Row) -> DatasetEntry:
    return DatasetEntry(
        id=r.id,
        root=r.root,
        path=r.relpath,
        name=r.name,
        kind=r.kind,
        state=r.state,
        reason=r.reason,
        size_bytes=r.size_bytes,
        mtime=r.mtime,
        version=r.version,
        summary=r.summary,
    )


def clean_folder(path: str) -> str:
    """A folder key as stored in the index; rejects anything that is not a plain relative path."""
    if len(path) > MAX_PATH or "\0" in path or "\\" in path:
        raise ApiError(400, "invalid_folder", "The folder path is not a plain relative path.")
    parts = [p for p in path.strip("/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ApiError(400, "invalid_folder", "The folder path is not a plain relative path.")
    return "/".join(parts)


@router.get("/roots")
def roots(request: Request) -> list[RootEntry]:
    cat = _catalog(request)
    counts = cat.db.root_counts()
    return [
        RootEntry(name=n, n_datasets=sum(counts.get(n, {}).values()), states=counts.get(n, {}))
        for n in cat.roots
    ]


@router.get("/tree")
def tree(request: Request, root: str, path: str = "") -> FolderListing:
    cat = _catalog(request)
    if root not in cat.roots:
        raise ApiError(404, "unknown_root", "No data root with this name.")
    folder = clean_folder(path)
    rows, sub = cat.db.folder(root, folder)
    if folder and not rows and not sub:
        raise ApiError(404, "unknown_folder", "No indexed dataset is in this folder.")
    return FolderListing(
        root=root,
        path=folder,
        folders=[
            FolderEntry(name=n, path=f"{folder}/{n}" if folder else n, n_datasets=k)
            for n, k in sub.items()
        ],
        datasets=[_entry(r) for r in rows],
    )


@router.get("/search")
def search(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    root: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[DatasetEntry]:
    cat = _catalog(request)
    if root is not None and root not in cat.roots:
        raise ApiError(404, "unknown_root", "No data root with this name.")
    rows = cat.db.search(q, root=root, limit=limit)
    return [_entry(r) for r in rows if r.root in cat.roots]


@router.get("/datasets/{id}")
def dataset(request: Request, id: str) -> DatasetDetail:
    row = _catalog(request).get(id)
    if row is None:
        raise ApiError(404, "unknown_dataset", "No dataset with this id in the catalog.")
    fp = json.loads(row.fingerprint) if row.fingerprint else None
    if fp is not None:
        fp["path"] = row.relpath
    from canopy.jobs.api import conversion_state  # the jobs API sits on top of the catalog

    return DatasetDetail(
        **_entry(row).model_dump(),
        fingerprint=fp,
        conversion=conversion_state(request, row.id, row.version)
        if hasattr(request.app.state, "jobs")
        else None,
    )


@router.get("/status")
def status(request: Request) -> CatalogStatus:
    cat = _catalog(request)
    return CatalogStatus(scanning=cat.scanning, last_scan=cat.db.last_scan())


@router.post("/refresh", status_code=202)
def refresh(request: Request, full: bool = False) -> dict[str, Any]:
    cat = _catalog(request)
    return {"started": cat.refresh(full=full)}
