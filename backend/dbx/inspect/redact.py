"""Privacy levels for fingerprints leaving the host.

`standard` keeps names (fingerprints never contain values). `strict` replaces every
user-chosen name (paths, file and element names, columns, keys, channels, coordinate systems)
by a stable salted hash, so the same name maps to the same token within one installation and
structure stays comparable across bundles. Structural names defined by the formats are kept.

The salt is a random secret created once per installation and kept on the host (never written
into a bundle). Without it, tokens cannot be reversed by hashing a list of likely names.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Any

from dbx.config import PRIVACY_LEVELS
from dbx.inspect.models import Fingerprint

SALT_FILE = ".dbx_salt"

# Names defined by the file formats or near-universal conventions: kept in strict mode.
KEEP = frozenset(
    {
        "global",
        "x",
        "y",
        "z",
        "geometry",
        "radius",
        "X_umap",
        "X_pca",
        "X_tsne",
        "spatial",
        "X_spatial",
        "spatialdata_attrs",
        "_index",
    }
)


def token(name: str, salt: str) -> str:
    if name in KEEP:
        return name
    return "h_" + hashlib.sha256(f"{salt}|{name}".encode()).hexdigest()[:10]


def installation_salt(folder: Path) -> str:
    """Read the installation's secret salt from `folder`, creating it (mode 600) on first use."""
    path = folder / SALT_FILE
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        pass
    folder.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # created concurrently by another process
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    return value


def _rename_keys(d: dict[str, Any], salt: str) -> dict[str, Any]:
    return {token(k, salt): v for k, v in d.items()}


def _anndata(ad: dict[str, Any], salt: str) -> None:
    for frame in ("obs", "var"):
        f = ad[frame]
        for col in f["columns"]:
            col["name"] = token(col["name"], salt)
        if f.get("index_name"):
            f["index_name"] = token(f["index_name"], salt)
    for key in ("layers", "obsm", "varm", "obsp", "colors"):
        ad[key] = _rename_keys(ad[key], salt)
    ad["uns_keys"] = [token(k, salt) for k in ad["uns_keys"]]


def _transforms(items: list[dict[str, Any]], salt: str) -> None:
    for t in items:
        t["output"] = token(t["output"], salt)


def redact(fp: Fingerprint, level: str, salt: str) -> Fingerprint:
    """Apply a privacy level. Unknown levels are errors (a typo must never mean "no redaction")."""
    if level not in PRIVACY_LEVELS:
        raise ValueError(f"unknown privacy level {level!r}; expected one of {PRIVACY_LEVELS}")
    if level != "strict":
        return fp
    if not salt:
        raise ValueError("strict redaction needs a non-empty salt")
    d = fp.model_dump()
    d["path"] = token(d["path"], salt)
    d["name"] = token(d["name"], salt)
    if d.get("anndata"):
        _anndata(d["anndata"], salt)
    sd = d.get("spatialdata")
    if sd:
        for kind in ("images", "labels"):
            for img in sd[kind].values():
                _transforms(img["transforms"], salt)
                if img.get("channel_names"):
                    img["channel_names"] = [token(c, salt) for c in img["channel_names"]]
            sd[kind] = _rename_keys(sd[kind], salt)
        for kind in ("points", "shapes"):
            for el in sd[kind].values():
                _transforms(el["transforms"], salt)
                el["columns"] = _rename_keys(el["columns"], salt)
                for k in ("feature_key", "instance_key"):
                    if el.get(k):
                        el[k] = token(el[k], salt)
            sd[kind] = _rename_keys(sd[kind], salt)
        for t in sd["tables"].values():
            _anndata(t, salt)
        for ref in sd["table_refs"].values():
            ref["region"] = [token(r, salt) for r in ref["region"]]
            for k in ("region_key", "instance_key"):
                if ref.get(k):
                    ref[k] = token(ref[k], salt)
        sd["tables"] = _rename_keys(sd["tables"], salt)
        sd["table_refs"] = _rename_keys(sd["table_refs"], salt)
        sd["coordinate_systems"] = [token(c, salt) for c in sd["coordinate_systems"]]
    for w in d["warnings"]:
        if w.get("element"):
            w["element"] = token(w["element"], salt)
    for e in d["errors"]:
        e["element"] = token(e["element"], salt)
        e["detail"] = None
    d["redacted"] = True
    return Fingerprint.model_validate(d)
