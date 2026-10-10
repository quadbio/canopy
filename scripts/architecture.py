"""Generate the architecture map: docs/architecture/index.html.

Reads the code itself, so the page stays true to the implementation:
- backend (Python, `ast`): modules with their docstrings, classes, methods, functions,
  HTTP routes, and which dbx modules each imports;
- frontend (TypeScript): files with their header comments, exported names, and relative imports.

Run: `pixi run -e dev python scripts/architecture.py` (also a pixi task: `architecture`).
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend" / "dbx"
FRONTEND = REPO / "frontend" / "src"
OUT = REPO / "docs" / "architecture" / "index.html"  # standalone page (open in a browser)
OUT_BODY = REPO / "docs" / "architecture" / "artifact.html"  # body only, for publishing
TEMPLATE = Path(__file__).with_name("architecture_template.html")

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "api_route"}


def first_paragraph(doc: str | None, limit: int = 600) -> str:
    if not doc:
        return ""
    para = doc.strip().split("\n\n", 1)[0]
    text = " ".join(line.strip() for line in para.splitlines())
    return text[:limit] + ("…" if len(text) > limit else "")


def signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = []
    a = fn.args
    for arg in [*a.posonlyargs, *a.args]:
        if arg.arg in ("self", "cls"):
            continue
        args.append(arg.arg)
    if a.vararg:
        args.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        args.append("*")
    args += [k.arg for k in a.kwonlyargs]
    if a.kwarg:
        args.append("**" + a.kwarg.arg)
    ret = f" → {ast.unparse(fn.returns)}" if fn.returns is not None else ""
    return f"({', '.join(args)}){ret}"


def routes_of(fn: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str) -> list[str]:
    out = []
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in HTTP_METHODS:
            path = d.args[0].value if d.args and isinstance(d.args[0], ast.Constant) else ""
            if d.func.attr == "api_route":
                methods = next(
                    (
                        [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
                        for kw in d.keywords
                        if kw.arg == "methods" and isinstance(kw.value, ast.List)
                    ),
                    ["GET"],
                )
                verb = "/".join(str(m) for m in methods)
            else:
                verb = d.func.attr.upper()
            base = prefix if isinstance(d.func.value, ast.Name) and d.func.value.id == "router" else ""
            out.append(f"{verb} {base}{path}")
    return out


def backend_module(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text())
    rel = path.relative_to(BACKEND.parent).with_suffix("")
    name = ".".join(rel.parts)
    if name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    prefix = ""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "router" for t in node.targets
        ):
            call = node.value
            if isinstance(call, ast.Call):
                for kw in call.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        prefix = str(kw.value.value)
    imports: list[list[Any]] = []  # [module, [names]]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("dbx"):
            imports.append([node.module, [a.name for a in node.names]])
        elif isinstance(node, ast.Import):
            imports += [[a.name, []] for a in node.names if a.name.startswith("dbx.")]
    classes, functions, routes = [], [], []

    def visit_fn(fn: ast.FunctionDef | ast.AsyncFunctionDef, nested_in: str | None = None) -> dict[str, Any]:
        r = routes_of(fn, prefix)
        item = {
            "name": fn.name,
            "sig": signature(fn),
            "doc": first_paragraph(ast.get_docstring(fn), 300),
            "routes": r,
            "private": fn.name.startswith("_"),
            "line": fn.lineno,
        }
        for route in r:
            routes.append({"route": route, "handler": f"{name}.{fn.name}", "doc": item["doc"], "line": fn.lineno})
        # routes and helpers defined inside factory functions (create_app)
        for sub in fn.body:
            if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and routes_of(sub, prefix):
                visit_fn(sub, fn.name)
        return item

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            methods = [
                visit_fn(m)
                for m in node.body
                if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            fields = [
                ast.unparse(s.target)
                for s in node.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            ]
            classes.append(
                {
                    "name": node.name,
                    "bases": bases,
                    "doc": first_paragraph(ast.get_docstring(node), 400),
                    "methods": methods,
                    "fields": fields[:30],
                    "line": node.lineno,
                }
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.append(visit_fn(node))
    loc = len(path.read_text().splitlines())
    return {
        "id": name,
        "side": "backend",
        "package": name.split(".")[1] if name.count(".") >= 2 else "core",
        "file": str(path.relative_to(REPO)),
        "doc": first_paragraph(ast.get_docstring(tree)),
        "classes": classes,
        "functions": functions,
        "routes": routes,
        "imports": imports,
        "loc": loc,
    }


TS_EXPORT = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?(function|class|const|interface|type)\s+([A-Za-z0-9_]+)"
    r"(?:\s*(?:<[^>]*>)?\s*(\([^)]*\)))?",
    re.M,
)
TS_IMPORT = re.compile(r"""^import\s+(?:type\s+)?[^'"]*?from\s+['"](\.[^'"]+)['"]|^import\s+['"](\.[^'"]+)['"]""", re.M)
TS_COMMENT = re.compile(r"^((?:\s*//[^\n]*\n)+)")
TS_DOC = re.compile(r"/\*\*\s*(.*?)\s*\*/\s*\n\s*export\s+(?:default\s+)?(?:async\s+)?\w+\s+([A-Za-z0-9_]+)", re.S)
TS_WORKER = re.compile(r"new Worker\(new URL\('(\.[^']+)'")
TS_REGISTER = re.compile(r"registerView<\w+>\(\{\s*type:\s*'([^']+)'")


def ts_module(path: Path) -> dict[str, Any]:
    text = path.read_text()
    rel = path.relative_to(FRONTEND)
    mod_id = "src/" + str(rel.with_suffix("")).replace("\\", "/")
    m = TS_COMMENT.match(text)
    header = " ".join(l.strip().lstrip("/").strip() for l in m.group(1).splitlines()) if m else ""
    docs = {name: " ".join(d.replace("*", " ").split()) for d, name in TS_DOC.findall(text)}
    exports = []
    for kind, name, params in TS_EXPORT.findall(text):
        exports.append({"kind": kind, "name": name, "sig": params or "", "doc": docs.get(name, "")})
    imports = set()
    for a, b in TS_IMPORT.findall(text):
        spec = a or b
        target = (path.parent / spec).resolve()
        for cand in (target, target.with_suffix(".ts"), target.with_suffix(".tsx"), target / "index.tsx", target / "index.ts"):
            if cand.is_file():
                imports.add("src/" + str(cand.relative_to(FRONTEND).with_suffix("")).replace("\\", "/"))
                break
    registered = TS_REGISTER.findall(text)
    workers = [
        "src/" + str((path.parent / w).resolve().relative_to(FRONTEND).with_suffix("")).replace("\\", "/")
        for w in TS_WORKER.findall(text)
    ]
    top = rel.parts[0] if len(rel.parts) > 1 else "app"
    return {
        "id": mod_id,
        "side": "frontend",
        "package": top,
        "file": str(path.relative_to(REPO)),
        "doc": header[:600],
        "exports": exports,
        "imports": sorted(imports),
        "registers": registered,
        "workers": workers,
        "loc": len(text.splitlines()),
    }


def _node(mod_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", mod_id)


# Used by nearly every module: drawing those edges hides the structure. The explorer lists them.
FOUNDATION = {"dbx.logs", "dbx.config", "dbx.build", "dbx.privacy", "dbx.environment"}
ENTRY_POINTS = {"dbx.cli"}


def dependency_graph(modules: list[dict[str, Any]], side: str) -> str:
    """Mermaid flowchart of one side: a subgraph per package, an edge per import (backend:
    without edges into the foundation modules and out of the CLI)."""
    mods = [m for m in modules if m["side"] == side]
    lines = ["flowchart LR" if side == "frontend" else "flowchart TB"]
    packages: dict[str, list[dict[str, Any]]] = {}
    for m in mods:
        packages.setdefault(m["package"], []).append(m)
    for pkg, items in sorted(packages.items()):
        lines.append(f'  subgraph {_node("pkg_" + side + pkg)}["{pkg}"]')
        for m in items:
            label = m["id"].split(".")[-1] if side == "backend" else m["id"].removeprefix("src/")
            lines.append(f'    {_node(m["id"])}["{label}"]')
        lines.append("  end")
    for m in mods:
        if m["id"] in ENTRY_POINTS:
            continue
        for imp in m["imports"]:
            if imp not in FOUNDATION:
                lines.append(f"  {_node(m['id'])} --> {_node(imp)}")
        for w in m.get("workers", []):
            lines.append(f"  {_node(m['id'])} -. starts .-> {_node(w)}")
    for m in mods:
        lines.append(f'  click {_node(m["id"])} call showModule("{m["id"]}")')
    return "\n".join(lines)


def main() -> None:
    modules = [backend_module(p) for p in sorted(BACKEND.rglob("*.py")) if "__pycache__" not in p.parts]
    modules = [m for m in modules if m["classes"] or m["functions"] or m["doc"] or m["imports"]]
    modules += [
        ts_module(p)
        for p in sorted(FRONTEND.rglob("*.ts*"))
        if not p.name.endswith((".test.ts", ".test.tsx", ".d.ts"))
    ]
    ids = {m["id"] for m in modules}
    # `from dbx.catalog import Catalog` names the package; follow its __init__ re-exports to the
    # module that defines the name, so edges connect real modules.
    reexports: dict[str, dict[str, str]] = {}
    for m in modules:
        if m["side"] == "backend":
            for mod, names in m["imports"]:
                for n in names:
                    reexports.setdefault(m["id"], {})[n] = mod
    for m in modules:
        if m["side"] != "backend":
            continue
        resolved: set[str] = set()
        for mod, names in m["imports"]:
            for n in names or [""]:
                target = reexports.get(mod, {}).get(n, mod) if mod != m["id"] else mod
                if f"{mod}.{n}" in ids:  # `from dbx.catalog import api`
                    target = f"{mod}.{n}"
                if target in ids:
                    resolved.add(target)
        m["imports"] = sorted(resolved - {m["id"]})
    # Package __init__ files only re-export: leave them out of the map.
    modules = [
        m
        for m in modules
        if not (m["side"] == "backend" and m["file"].endswith("__init__.py") and m["id"] != "dbx")
    ]
    ids = {m["id"] for m in modules}
    for m in modules:
        m["imports"] = [i for i in m["imports"] if i in ids]
        m["used_by"] = sorted(o["id"] for o in modules if m["id"] in o["imports"])
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    data = {"commit": commit, "modules": modules}
    html = (
        TEMPLATE.read_text()
        .replace("/*__DATA__*/null", json.dumps(data).replace("</", "<\\/"))
        .replace("%%BACKEND_GRAPH%%", dependency_graph(modules, "backend"))
        .replace("%%FRONTEND_GRAPH%%", dependency_graph(modules, "frontend"))
        .replace("%%COMMIT%%", commit)
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT_BODY.write_text(html)
    OUT.write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n</head>\n<body>\n'
        + html
        + "\n</body>\n</html>\n"
    )
    n_routes = sum(len(m.get("routes", [])) for m in modules)
    print(f"{OUT.relative_to(REPO)}: {len(modules)} modules, {n_routes} routes, commit {commit}")


if __name__ == "__main__":
    main()
