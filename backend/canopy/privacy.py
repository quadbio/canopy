"""Privacy primitives shared by fingerprints and logs: the installation salt, name tokens and
redaction of free text (exception and warning messages).

Levels (config `logging.privacy`, fingerprint `--level`):
- `debug`: everything kept; for the developer's own machine only.
- `standard`: names kept, values never. Free text cannot be split into names and values, so
  quoted literals in messages are replaced by tokens and long messages are cut.
- `strict`: every user-chosen name becomes a salted token; free text is dropped.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path

SALT_FILE = ".canopy_salt"

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

MESSAGE_LIMIT = 300
# A quote opens only where a word does not continue (the apostrophe in "Can't" is not a quote).
_QUOTED = re.compile(r"""(?<!\w)(['"])((?:(?!\1)[^\n]){1,500}?)\1(?!\w)""")
# Absolute POSIX paths of at least two components (/home/u/x.h5ad, /data/a/b).
_PATH = re.compile(r"(?<![\w.])/(?:[^\s/'\"():,]+/)+[^\s/'\"():,]*")


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


def redact_text(text: str, level: str, salt: str) -> str | None:
    """Free text (an exception or warning message) at a privacy level; None when dropped."""
    if level == "debug":
        return text
    if level == "strict":
        return None
    text = _PATH.sub(lambda m: "<path:" + token(m.group(0), salt) + ">", text)
    text = _QUOTED.sub(lambda m: m.group(1) + token(m.group(2), salt) + m.group(1), text)
    if len(text) > MESSAGE_LIMIT:
        text = text[:MESSAGE_LIMIT] + "…"
    return text


def scrub_text(text: str) -> str:
    """Free text that may be stored or shown (e.g. an element's error detail): absolute paths are
    replaced by `<path>`; names in it are kept (the `standard` rule)."""
    return _PATH.sub("<path>", text)


def code_location(filename: str) -> str:
    """A source file as a location inside the installed software (no home or install prefix)."""
    parts = Path(filename).parts
    for anchor in ("site-packages", "backend"):
        if anchor in parts:
            return "/".join(parts[parts.index(anchor) + 1 :])
    return "/".join(parts[-2:])
