"""`canopy analyse`: a diagnostics bundle (from `canopy bundle`, made on the host) as a readable
report, on the developer's computer: problems first, then timings against the performance
targets, conversions, the index, memory and the browser's measurements; with `--previous`, the
key numbers next to those of an earlier bundle.

The bundle is read in memory (never extracted). It holds codes, numbers and tokens only (strict
privacy), so the report does too.
"""

from __future__ import annotations

import io
import json
import tarfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_MEMBER_BYTES = 256 * 2**20  # a bundle is a few MB; anything this large is not one

# Performance targets (reports/09_consolidated_summary.md, "Performance targets"): (what, the
# measurement, the limit in ms, how it is computed).
TARGETS = (
    ("Embedding of ≥ 0.5 M cells ready (90 % of views)", "embedding_ready_p90", 3000.0),
    ("Gene colouring, ≤ 1.2 M cells (90 % of switches)", "gene_small_p90", 300.0),
    ("Gene colouring, > 1.2 M cells (90 % of switches)", "gene_large_p90", 1000.0),
    ("Frame while moving (median of drags; ≥ 30 frames/s)", "frame_moving_median", 33.3),
)


@dataclass
class Bundle:
    """What a bundle holds, parsed."""

    manifest: str = ""
    host: dict[str, Any] = field(default_factory=dict)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    catalog: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    crashes: dict[str, str] = field(default_factory=dict)  # non-empty crash files
    unreadable_lines: int = 0


def read_bundle(path: Path) -> Bundle:
    b = Bundle()
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or m.size > MAX_MEMBER_BYTES:
                continue
            name = m.name.split("/", 1)[1] if "/" in m.name else m.name
            f = tar.extractfile(m)
            if f is None:
                continue
            text = f.read().decode(errors="replace")
            if name == "MANIFEST.md":
                b.manifest = text
            elif name == "host.json":
                b.host = json.loads(text)
            elif name == "jobs.json":
                b.jobs = json.loads(text)
            elif name == "catalog.json":
                b.catalog = json.loads(text)
            elif name.startswith("crash/") and text.strip():
                b.crashes[name.removeprefix("crash/")] = text
            elif name.startswith("logs/"):
                for line in io.StringIO(text):
                    try:
                        b.events.append(json.loads(line))
                    except ValueError:
                        b.unreadable_lines += 1
    return b


# ---------------------------------------------------------------- numbers


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def _stats(xs: Iterable[float | int | None]) -> dict[str, float | int | None]:
    v = [float(x) for x in xs if isinstance(x, (int, float))]
    return {
        "n": len(v),
        "p50": _pct(v, 0.5),
        "p90": _pct(v, 0.9),
        "max": max(v) if v else None,
    }


def _client(b: Bundle, name: str) -> list[dict[str, Any]]:
    return [
        e for e in b.events if e.get("event") == "client_event" and e.get("client_event") == name
    ]


def metrics(b: Bundle) -> dict[str, Any]:
    """The numbers the report shows (also what `--previous` compares)."""
    ev = b.events
    m: dict[str, Any] = {}
    m["builds"] = sorted({e["build"] for e in ev if isinstance(e.get("build"), str)})
    m["server_starts"] = sum(e.get("event") == "startup" and e.get("role") == "server" for e in ev)
    times = sorted(e["timestamp"] for e in ev if isinstance(e.get("timestamp"), str))
    m["span"] = (times[0], times[-1]) if times else None

    # problems
    levels: Counter[tuple[str, str, str]] = Counter()
    for e in ev:
        if e.get("level") in ("warning", "error", "critical"):
            what = e.get("code") or e.get("exc_type") or e.get("reason") or e.get("logger") or ""
            levels[(e["level"], str(e.get("event")), str(what))] += 1
    m["problems"] = sorted(levels.items(), key=lambda kv: (-kv[1], kv[0]))
    m["stage_failed"] = Counter(
        (str(e.get("stage")), str(e.get("exc_type")), str((e.get("exc_frames") or ["?"])[-1]))
        for e in ev
        if e.get("event") == "stage_failed"
    ).most_common()
    js: Counter[tuple[str, str, str, str]] = Counter()
    for name in ("js_error", "js_unhandled_rejection", "deck_error"):
        for e in _client(b, name):
            js[
                (name, str(e.get("c_message")), str(e.get("c_where")), str(e.get("c_caller", "")))
            ] += 1
    m["browser_errors"] = js.most_common()
    m["server_errors"] = sum(
        e.get("event") == "request" and isinstance(e.get("status"), int) and e["status"] >= 500
        for e in ev
    )

    # requests and stages
    by_route: dict[str, list[float]] = defaultdict(list)
    for e in ev:
        if e.get("event") == "request" and e.get("route") != "<static>":
            by_route[str(e.get("route"))].append(e.get("ms") or 0)
    m["routes"] = {r: _stats(v) for r, v in by_route.items()}
    by_stage: dict[str, list[float]] = defaultdict(list)
    for e in ev:
        if e.get("event") == "stage":
            by_stage[f"{e.get('role')}:{e.get('stage')}"].append(e.get("ms") or 0)
    m["stages"] = {s: _stats(v) for s, v in by_stage.items()}

    # memory
    def most(event: str, key: str, role: str | None = None) -> float | None:
        v = [
            e[key]
            for e in ev
            if e.get("event") == event
            and isinstance(e.get(key), (int, float))
            and (role is None or e.get("role") == role)
        ]
        return max(v) if v else None

    least_free = [
        e["host_available_gb"]
        for e in ev
        if e.get("event") == "memory_sample"
        and isinstance(e.get("host_available_gb"), (int, float))
    ]
    m["memory"] = {
        "server_rss_mb_max": most("memory_sample", "rss_mb", "server"),
        "worker_rss_mb_max": most("job_progress", "rss_mb"),
        "cgroup_usage_gb_max": most("memory_sample", "cgroup_usage_gb"),
        "host_available_gb_min": min(least_free) if least_free else None,
    }

    # conversions
    jobs = b.jobs
    m["jobs"] = {
        "n": len(jobs),
        "states": dict(Counter(j.get("state") for j in jobs)),
        "errors": dict(Counter(j.get("error") for j in jobs if j.get("error"))),
        "stop_reasons": dict(Counter(j.get("stop_reason") for j in jobs if j.get("stop_reason"))),
        "seconds": _stats((j.get("result") or {}).get("seconds") for j in jobs),
        "peak_rss_gb": _stats((j.get("result") or {}).get("peak_rss_gb") for j in jobs),
        "wait_s": _stats(
            j["started"] - j["created"] for j in jobs if j.get("started") and j.get("created")
        ),
        "warnings": dict(
            Counter(w for j in jobs for w in (j.get("result") or {}).get("warnings") or [])
        ),
    }
    m["slowest_jobs"] = sorted(
        (
            {
                "job": j.get("id"),
                "seconds": (j.get("result") or {}).get("seconds"),
                "peak_rss_gb": (j.get("result") or {}).get("peak_rss_gb"),
                "kind": "spatial"
                if (j.get("result") or {}).get("images") is not None
                else "anndata",
            }
            for j in jobs
            if isinstance((j.get("result") or {}).get("seconds"), (int, float))
        ),
        key=lambda x: -float(x["seconds"] or 0),
    )[:5]

    # index
    m["catalog"] = {
        "n": len(b.catalog),
        "kinds_states": dict(Counter(f"{c.get('kind')}/{c.get('state')}" for c in b.catalog)),
        "reasons": dict(Counter(c.get("reason") for c in b.catalog if c.get("reason"))),
        "size_gb": round(sum(c.get("size_bytes") or 0 for c in b.catalog) / 2**30, 1),
        "inspect_ms": _stats(c.get("inspect_ms") for c in b.catalog),
    }

    # the browser
    client: dict[str, list[float]] = defaultdict(list)
    for e in ev:
        if e.get("event") == "client_event" and isinstance(e.get("ms"), (int, float)):
            client[str(e.get("client_event"))].append(e["ms"])
    m["client"] = {k: _stats(v) for k, v in client.items()}
    frames = [e for e in _client(b, "frames") if (e.get("c_n_moving") or 0) > 0]
    m["frames_moving"] = _stats(e.get("c_moving_median_ms") for e in frames)
    m["frames_moving_p95"] = _stats(e.get("c_moving_p95_ms") for e in frames)
    by_views: dict[str, list[float]] = defaultdict(list)
    for e in frames:
        v = e.get("c_views") or 1
        bucket = "1" if v <= 1 else "2–4" if v <= 4 else "5–9" if v <= 9 else "10+"
        by_views[bucket].append(e.get("c_moving_median_ms") or 0)
    m["frames_by_views"] = {k: _stats(v) for k, v in sorted(by_views.items())}
    m["lod_budget"] = len(_client(b, "lod_budget"))
    m["groups_opened"] = [
        (e.get("c_type"), e.get("c_n"), e.get("c_skipped")) for e in _client(b, "group_opened")
    ]
    m["lasso"] = [(e.get("c_n"), e.get("c_selected"), e.get("ms")) for e in _client(b, "lasso")]
    m["screens"] = sorted(
        {
            f"{e.get('c_viewport_w')}×{e.get('c_viewport_h')} @{e.get('c_dpr')}"
            for e in _client(b, "page_load")
        }
    )
    m["gpus"] = sorted({str(e.get("c_gpu")) for e in _client(b, "environment")})

    # targets
    emb = [
        e["ms"]
        for e in _client(b, "view_ready")
        if e.get("c_type") == "embedding" and (e.get("c_n") or 0) >= 500_000 and e.get("ms")
    ]
    # a gene chosen in a linked group is loaded by every member at once (the browser queues the
    # requests): those are one switch each, reported apart; older bundles do not tell them apart
    all_genes = _client(b, "gene_loaded")
    genes = [e for e in all_genes if not e.get("c_linked")]
    m["gene_linked"] = _stats(e.get("ms") for e in all_genes if e.get("c_linked"))
    small = [e["ms"] for e in genes if (e.get("c_n") or 0) <= 1_200_000 and e.get("ms")]
    large = [e["ms"] for e in genes if (e.get("c_n") or 0) > 1_200_000 and e.get("ms")]
    m["targets"] = {
        "embedding_ready_p90": (_pct(emb, 0.9), len(emb)),
        "gene_small_p90": (_pct(small, 0.9), len(small)),
        "gene_large_p90": (_pct(large, 0.9), len(large)),
        "frame_moving_median": (m["frames_moving"]["p50"], m["frames_moving"]["n"]),
    }
    m["crash_files"] = sorted(b.crashes)
    return m


# ---------------------------------------------------------------- report


def _ms(x: float | int | None) -> str:
    if x is None:
        return "–"
    return f"{x / 1000:.1f} s" if x >= 10_000 else f"{x:.0f} ms"


def _num(x: Any) -> str:
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:.2f}" if abs(x) < 100 else f"{x:.0f}"
    return str(x)


def _table(head: list[str], rows: Iterable[list[str]]) -> list[str]:
    body = ["| " + " | ".join(r) + " |" for r in rows]
    if not body:
        return ["(none)"]
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head), *body]


def report(b: Bundle, m: dict[str, Any], prev: dict[str, Any] | None = None) -> str:
    lines: list[str] = ["# Canopy bundle report", ""]
    first = next((ln for ln in b.manifest.splitlines() if ln.startswith("Created")), "")
    if first:
        lines += [first, ""]
    env = b.host.get("environment", {})
    span = m["span"]
    lines += [
        f"- Builds: {', '.join(m['builds']) or '–'}; server starts: {m['server_starts']}",
        f"- Logs from {span[0][:16]} to {span[1][:16]} (UTC)" if span else "- No log lines",
        f"- Host: {env.get('cpu_count')} cores, {env.get('memory_total_gb')} GB, "
        f"{(env.get('platform') or '')[:40]}; cache on {(env.get('cache_fs') or {}).get('fstype')}",
        f"- Browser screens: {', '.join(m['screens']) or '–'}; GPUs: {', '.join(m['gpus']) or '–'}",
        "",
    ]

    # problems first
    lines += ["## Problems", ""]
    problems = []
    if m["server_errors"]:
        problems.append(f"- **{m['server_errors']} server errors (5xx)**")
    if m["crash_files"]:
        problems.append(
            "- **Crash or stack-dump files with content** (a crash, a hang, a worker error, or "
            f"a dump asked for with SIGUSR1): {', '.join(m['crash_files'])}"
        )
    failed = {
        s: n for s, n in m["jobs"]["states"].items() if s not in ("done", "queued", "running")
    }
    if failed:
        problems.append(f"- **Conversions not done:** {failed}; errors {m['jobs']['errors']}")
    for (stage, exc, frame), n in m["stage_failed"]:
        problems.append(f"- stage_failed ×{n}: {stage}, {exc} at {frame}")
    for (name, msg, where, caller), n in m["browser_errors"]:
        problems.append(f"- browser {name} ×{n}: {msg} at {where} (from {caller or '?'})")
    lines += problems or ["None found: no 5xx, crashes, failed conversions or browser errors."]
    if m["problems"]:
        lines += ["", "Warnings and errors in the logs:", ""]
        lines += _table(
            ["level", "event", "code / type", "n"],
            ([lv, e, w, str(n)] for (lv, e, w), n in m["problems"][:25]),
        )
    lines.append("")

    # targets
    lines += ["## Performance targets", ""]
    rows = []
    for label, key, limit in TARGETS:
        value, n = m["targets"][key]
        verdict = "no data" if value is None else "met" if value <= limit else "**missed**"
        rows.append([label, f"{_ms(limit)}", _ms(value), str(n), verdict])
    lines += _table(["target", "limit", "measured", "samples", ""], rows)
    gl = m["gene_linked"]
    if gl["n"]:
        lines += [
            "",
            f"Genes loaded in linked groups (every member at once, not counted above): {gl['n']}, "
            f"median {_ms(gl['p50'])}, slowest {_ms(gl['max'])}.",
        ]
    lines.append("")

    # browser
    lines += ["## In the browser", ""]
    lines += _table(
        ["views moving together", "drags", "frame (median of drags)", "90 % of drags", "slowest"],
        (
            [k, str(s["n"]), _ms(s["p50"]), _ms(s["p90"]), _ms(s["max"])]
            for k, s in m["frames_by_views"].items()
        ),
    )
    lines += [
        "",
        f"Detail lowered by the frame budget: {m['lod_budget']} times. Linked groups opened: "
        + (
            ", ".join(
                f"{t} ×{n}" + (f" ({s} skipped)" if s else "") for t, n, s in m["groups_opened"]
            )
            or "none"
        )
        + ". Lasso: "
        + (
            ", ".join(f"{_num(sel)} of {_num(n)} in {_ms(ms)}" for n, sel, ms in m["lasso"])
            or "none"
        )
        + ".",
        "",
    ]
    keys = [k for k in m["client"] if m["client"][k]["n"]]
    lines += _table(
        ["browser step", "n", "median", "90 %", "slowest"],
        (
            [k, str(s["n"]), _ms(s["p50"]), _ms(s["p90"]), _ms(s["max"])]
            for k, s in sorted(
                ((k, m["client"][k]) for k in keys), key=lambda kv: -(kv[1]["max"] or 0)
            )
        ),
    )
    lines.append("")

    # server
    lines += ["## Server", ""]
    lines += _table(
        ["route", "n", "median", "90 %", "slowest"],
        (
            [r, str(s["n"]), _ms(s["p50"]), _ms(s["p90"]), _ms(s["max"])]
            for r, s in sorted(m["routes"].items(), key=lambda kv: -(kv[1]["max"] or 0))[:15]
        ),
    )
    lines += ["", "Slowest stages:", ""]
    lines += _table(
        ["stage", "n", "median", "90 %", "slowest"],
        (
            [st, str(s["n"]), _ms(s["p50"]), _ms(s["p90"]), _ms(s["max"])]
            for st, s in sorted(m["stages"].items(), key=lambda kv: -(kv[1]["max"] or 0))[:15]
        ),
    )
    mem = m["memory"]
    lines += [
        "",
        f"Memory: server at most {_num(mem['server_rss_mb_max'])} MB; a conversion worker at most "
        f"{_num(mem['worker_rss_mb_max'])} MB; the account (cgroup) at most "
        f"{_num(mem['cgroup_usage_gb_max'])} GB; the host's free memory at least "
        f"{_num(mem['host_available_gb_min'])} GB.",
        "",
    ]

    # conversions and index
    j = m["jobs"]
    lines += [
        "## Conversions",
        "",
        f"{j['n']} jobs: {j['states']}"
        + (f"; stopped: {j['stop_reasons']}" if j["stop_reasons"] else "")
        + (f"; warnings: {j['warnings']}" if j["warnings"] else ""),
        f"Duration median {_num(j['seconds']['p50'])} s, slowest {_num(j['seconds']['max'])} s; "
        f"peak memory median {_num(j['peak_rss_gb']['p50'])} GB, "
        f"most {_num(j['peak_rss_gb']['max'])} GB; "
        f"waited in the queue up to {_num(j['wait_s']['max'])} s.",
        "",
    ]
    if m["slowest_jobs"]:
        lines += _table(
            ["job", "kind", "seconds", "peak GB"],
            (
                [str(x["job"]), x["kind"], _num(x["seconds"]), _num(x["peak_rss_gb"])]
                for x in m["slowest_jobs"]
            ),
        )
        lines.append("")
    c = m["catalog"]
    lines += [
        "## Index",
        "",
        f"{c['n']} datasets ({c['size_gb']} GB): {c['kinds_states']}"
        + (f"; reasons: {c['reasons']}" if c["reasons"] else "")
        + f". Inspection median {_ms(c['inspect_ms']['p50'])}, "
        f"slowest {_ms(c['inspect_ms']['max'])}.",
        "",
    ]

    if prev is not None:
        lines += ["## Compared with the previous bundle", ""]
        lines += _table(["measure", "previous", "this"], _compare(prev, m))
        lines.append("")
    if b.unreadable_lines:
        lines += [f"({b.unreadable_lines} log lines could not be read.)", ""]
    return "\n".join(lines)


def _compare(a: dict[str, Any], b: dict[str, Any]) -> list[list[str]]:
    rows: list[list[str]] = []
    for label, key, _ in TARGETS:
        rows.append([label, _ms(a["targets"][key][0]), _ms(b["targets"][key][0])])
    pairs: list[tuple[str, Callable[[dict[str, Any]], Any], Callable[[Any], str]]] = [
        ("Server errors (5xx)", lambda m: m["server_errors"], str),
        ("Browser errors", lambda m: sum(n for _, n in m["browser_errors"]), str),
        (
            "Conversions (done / all)",
            lambda m: f"{m['jobs']['states'].get('done', 0)} / {m['jobs']['n']}",
            str,
        ),
        ("Slowest conversion", lambda m: m["jobs"]["seconds"]["max"], lambda x: f"{_num(x)} s"),
        (
            "Server memory, most",
            lambda m: m["memory"]["server_rss_mb_max"],
            lambda x: f"{_num(x)} MB",
        ),
        ("Datasets in the index", lambda m: m["catalog"]["n"], str),
    ]
    for label, get, fmt in pairs:
        rows.append([label, fmt(get(a)), fmt(get(b))])
    return rows


def analyse(path: Path, previous: Path | None = None) -> str:
    b = read_bundle(path)
    m = metrics(b)
    prev = metrics(read_bundle(previous)) if previous else None
    return report(b, m, prev)
