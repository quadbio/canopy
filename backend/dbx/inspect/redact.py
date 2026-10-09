"""Privacy levels for fingerprints leaving the host.

`standard` keeps names (fingerprints never contain values). `strict` replaces every
user-chosen name (paths, file and element names, columns, keys, channels, coordinate systems)
by a stable salted hash, so the same name maps to the same token within one installation and
structure stays comparable across bundles. Structural names defined by the formats are kept.
"""

from __future__ import annotations

import hashlib
from typing import Any

from dbx.inspect.models import Fingerprint

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
    if level != "strict":
        return fp
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
    # Warnings quote element names; regenerate them from tokens is not possible, so drop names.
    d["warnings"] = [_strip_quoted(w) for w in d["warnings"]]
    d["redacted"] = True
    return Fingerprint.model_validate(d)


def _strip_quoted(text: str) -> str:
    parts = text.split("'")
    return "".join(p if i % 2 == 0 else "<name>" for i, p in enumerate(parts))
