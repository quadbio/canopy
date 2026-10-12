"""Job API: start a conversion, list and follow jobs, cancel.

- `POST /api/datasets/{id}/convert` `{"x_source": "X" | "layers/<name>", "feature_key"?}` → the
  job (an active
  job for the same dataset version and options is returned instead of a new one);
- `GET /api/jobs?dataset=` → recent jobs, newest first; `GET /api/jobs/{job}`;
- `POST /api/jobs/{job}/cancel`.
Jobs never show paths.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from dbx.catalog import PathEscapeError
from dbx.convert.anndata import has_store
from dbx.errors import ApiError
from dbx.jobs.db import ACTIVE
from dbx.jobs.manager import JobManager

router = APIRouter(tags=["jobs"])


class ConvertRequest(BaseModel):
    x_source: str = Field(default="X", max_length=512, pattern=r"^(X|raw/X|layers/.+)$")
    # SpatialData: the points column holding the genes, when the converter cannot tell.
    feature_key: str | None = Field(default=None, min_length=1, max_length=256)
    # Replace an existing converted copy (one per dataset version).
    force: bool = False


def _jobs(request: Request) -> JobManager:
    return request.app.state.jobs  # type: ignore[no-any-return]


@router.post("/api/datasets/{id}/convert")
def convert(request: Request, id: str, body: ConvertRequest | None = None) -> dict[str, Any]:
    body = body or ConvertRequest()
    catalog = request.app.state.catalog
    row = catalog.get(id)
    if row is None:
        raise ApiError(404, "unknown_dataset", "No dataset with this id in the catalog.")
    if row.state != "inspected" or not row.fingerprint or not row.version:
        raise ApiError(409, f"state:{row.state}", f"The dataset is {row.state}.")
    if row.kind not in ("h5ad", "anndata_zarr", "spatialdata_zarr"):
        raise ApiError(409, "not_convertible", "This kind of dataset cannot be converted.")
    try:
        path = catalog.resolve(row)
    except PathEscapeError as e:
        raise ApiError(403, "path_escape", "The dataset path leaves its data root.") from e
    except FileNotFoundError as e:
        raise ApiError(410, "vanished", "The dataset no longer exists.") from e
    cfg = request.app.state.config
    if has_store(cfg.cache_dir, row.version) and not body.force:
        raise ApiError(409, "already_converted", "This dataset is converted already.")
    options: dict[str, Any] = {"x_source": body.x_source}
    if body.feature_key:
        options["feature_key"] = body.feature_key
    job = _jobs(request).submit("convert", row.id, row.version, options, path, row.fingerprint)
    if job.options != options:
        raise ApiError(
            409, "conversion_running", "A conversion of this dataset with other options runs."
        )
    return job.public()


@router.get("/api/jobs")
def list_jobs(
    request: Request, dataset: str | None = None, active: bool = False
) -> list[dict[str, Any]]:
    """Recent jobs, newest first; `active=true`: only queued and running ones."""
    states = ACTIVE if active else ()
    return [j.public() for j in _jobs(request).db.find(dataset=dataset, states=states)[:50]]


@router.get("/api/jobs/{job}")
def get_job(request: Request, job: int) -> dict[str, Any]:
    j = _jobs(request).db.get(job)
    if j is None:
        raise ApiError(404, "unknown_job", "No such job.")
    return j.public()


@router.post("/api/jobs/{job}/cancel")
def cancel_job(request: Request, job: int) -> dict[str, Any]:
    j = _jobs(request).cancel(job)
    if j is None:
        raise ApiError(404, "unknown_job", "No such job.")
    return j.public()


def conversion_state(request: Request, dataset: str, version: str | None) -> dict[str, Any]:
    """For the catalog's dataset details: converted or not, and the latest job."""
    cfg = request.app.state.config
    jobs = _jobs(request).db.find(dataset=dataset)
    latest = next((j for j in jobs if j.version == version), None)
    converted = bool(version) and has_store(cfg.cache_dir, str(version))
    x_source = None
    if converted:
        from dbx.contract import read_manifest
        from dbx.convert.anndata import store_dir

        try:
            m = read_manifest(store_dir(cfg.cache_dir, str(version)))
            x_source = next(iter(m.tables.values())).x_source
        except (OSError, ValueError, StopIteration):
            x_source = None
    return {
        "converted": converted,
        "x_source": x_source,  # which matrix the converted copy holds
        "job": latest.public() if latest else None,
    }
