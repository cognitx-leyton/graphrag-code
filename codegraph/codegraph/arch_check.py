"""Architecture-conformance policies.

Runs a fixed set of built-in policies plus any user-authored policies from
``.arch-policies.toml`` as Cypher against the live Neo4j graph and returns an
:class:`ArchReport`. Mirrors :mod:`codegraph.validate`'s shape: typed result
dataclasses, Rich-table rendering when a console is attached, JSON
serialisation for CI, and an ``ok`` rollup that maps directly to a process
exit code.

Built-in policies:

- **import_cycles** — file IMPORTS cycles of configurable length.
- **cross_package** — forbidden import directions (configurable pair list).
- **layer_bypass** — controllers reaching ``*Repository`` methods without
  traversing a ``*Service`` (suffixes configurable).
- **coupling_ceiling** — files with more than N distinct file-level imports
  (configurable threshold).
- **orphan_detection** — functions, classes, atoms, or endpoints with zero
  inbound references and no framework-entry-point decorator.

User-authored policies live under ``[[policies.custom]]`` in
``.arch-policies.toml`` (see :mod:`codegraph.arch_config`).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from neo4j import Driver, GraphDatabase
from rich.console import Console
from rich.table import Table

from .arch_config import (
    ArchConfig,
    CouplingCeilingConfig,
    CrossPackageConfig,
    CustomPolicy,
    ImportCyclesConfig,
    LayerBypassConfig,
    OrphanDetectionConfig,
    load_arch_config,
)


# Sample-size cap per policy — keeps the report skimmable.
SAMPLE_LIMIT = 10


def _scope_filter(
    var: str, prop: str, scope: list[str] | None,
) -> tuple[str, dict]:
    """Return ``(cypher_fragment, params)`` for optional scope filtering.

    *var* is the Cypher variable name (e.g. ``"a"``), *prop* is the property
    to match (``"path"`` for :label:`File` nodes, ``"file"`` for
    :label:`Class`/:label:`Function`/:label:`Method` nodes).  Returns an
    empty fragment when *scope* is ``None`` or empty.
    """
    if not scope:
        return "", {}
    parts: list[str] = []
    params: dict[str, str] = {}
    for i, prefix in enumerate(scope):
        key = f"_scope{i}"
        parts.append(f"{var}.{prop} STARTS WITH ${key}")
        params[key] = prefix
    return f"({' OR '.join(parts)})", params


# ── Result shapes ────────────────────────────────────────────

@dataclass
class PolicyResult:
    """Outcome of a single architecture policy."""
    name: str
    passed: bool
    violation_count: int
    sample: list[dict] = field(default_factory=list)
    detail: str = ""


@dataclass
class ArchReport:
    """Aggregate result across all policies."""
    policies: list[PolicyResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(p.passed for p in self.policies)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "policies": [asdict(p) for p in self.policies],
            },
            indent=2,
            default=str,
        )


# ── Orchestrator ─────────────────────────────────────────────

def run_arch_check(
    uri: str,
    user: str,
    password: str,
    console: Optional[Console] = None,
    config: Optional[ArchConfig] = None,
    repo_root: Optional[Path] = None,
    scope: list[str] | None = None,
) -> ArchReport:
    """Open a driver, evaluate every configured policy, return an :class:`ArchReport`.

    ``config`` takes precedence if provided; otherwise :func:`load_arch_config`
    reads ``<repo_root>/.arch-policies.toml`` (``repo_root`` defaults to
    ``Path.cwd()``). Missing config file → all built-in defaults, no custom
    policies.

    ``scope``, when non-empty, restricts every built-in policy to nodes whose
    ``file`` / ``path`` property starts with at least one of the given
    prefixes.  Custom policies are *not* filtered — their Cypher is
    user-authored.
    """
    if config is None:
        config = load_arch_config(repo_root or Path.cwd())

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        policies = _run_all(driver, config, scope)
    finally:
        driver.close()

    report = ArchReport(policies=policies)
    if console is not None:
        _render(console, report)
    return report


def _run_all(
    driver: Driver, config: ArchConfig, scope: list[str] | None = None,
) -> list[PolicyResult]:
    """Evaluate every policy in a stable order. Disabled policies emit a marker."""
    out: list[PolicyResult] = []

    if config.import_cycles.enabled:
        out.append(_check_import_cycles(driver, config.import_cycles, scope))
    else:
        out.append(_disabled("import_cycles"))

    if config.cross_package.enabled:
        out.append(_check_cross_package(driver, config.cross_package, scope))
    else:
        out.append(_disabled("cross_package"))

    if config.layer_bypass.enabled:
        out.append(_check_layer_bypass(driver, config.layer_bypass, scope))
    else:
        out.append(_disabled("layer_bypass"))

    if config.coupling_ceiling.enabled:
        out.append(_check_coupling_ceiling(driver, config.coupling_ceiling, scope))
    else:
        out.append(_disabled("coupling_ceiling"))

    if config.orphan_detection.enabled:
        out.append(_check_orphans(driver, config.orphan_detection, scope))
    else:
        out.append(_disabled("orphan_detection"))

    for custom in config.custom:
        if custom.enabled:
            out.append(_check_custom(driver, custom))
        else:
            out.append(_disabled(custom.name))

    return out


def _disabled(name: str) -> PolicyResult:
    return PolicyResult(
        name=name,
        passed=True,
        violation_count=0,
        sample=[],
        detail="(disabled in .arch-policies.toml)",
    )


# ── Policies ─────────────────────────────────────────────────

def _check_import_cycles(
    driver: Driver, cfg: ImportCyclesConfig, scope: list[str] | None = None,
) -> PolicyResult:
    """Detect file-level IMPORTS cycles of configurable length."""
    hops = f"*{cfg.min_hops}..{cfg.max_hops}"
    scope_frag, scope_params = _scope_filter("a", "path", scope)
    where = f"WHERE {scope_frag}\n" if scope_frag else ""
    sample_cypher = (
        f"MATCH path = (a:File)-[:IMPORTS{hops}]->(a)\n"
        f"{where}"
        f"WITH [n IN nodes(path) | n.path] AS cycle, length(path) AS hops\n"
        f"RETURN DISTINCT cycle, hops\n"
        f"ORDER BY hops ASC, cycle[0]\n"
        f"LIMIT $limit"
    )
    count_cypher = (
        f"MATCH path = (a:File)-[:IMPORTS{hops}]->(a)\n"
        f"{where}"
        f"RETURN count(DISTINCT path) AS v"
    )
    with driver.session() as s:
        total = int(s.run(count_cypher, **scope_params).single()["v"] or 0)
        sample = [dict(r) for r in s.run(sample_cypher, limit=SAMPLE_LIMIT, **scope_params)]
    return PolicyResult(
        name="import_cycles",
        passed=(total == 0),
        violation_count=total,
        sample=sample,
        detail=f"Files (or packages) that import each other transitively (hops {cfg.min_hops}-{cfg.max_hops}).",
    )


def _check_cross_package(
    driver: Driver, cfg: CrossPackageConfig, scope: list[str] | None = None,
) -> PolicyResult:
    """Detect imports that cross a forbidden package boundary."""
    scope_frag, scope_params = _scope_filter("a", "path", scope)
    scope_clause = f" AND {scope_frag}" if scope_frag else ""

    detected: list[dict] = []
    total = 0
    with driver.session() as s:
        for pair in cfg.pairs:
            count = int(s.run(
                "MATCH (a:File)-[:IMPORTS]->(b:File) "
                "WHERE a.package = $a AND b.package = $b"
                f"{scope_clause} "
                "RETURN count(*) AS v",
                a=pair.importer, b=pair.importee, **scope_params,
            ).single()["v"] or 0)
            total += count
            if count and len(detected) < SAMPLE_LIMIT:
                rows = list(s.run(
                    "MATCH (a:File)-[:IMPORTS]->(b:File) "
                    "WHERE a.package = $a AND b.package = $b"
                    f"{scope_clause} "
                    "RETURN a.path AS importer, b.path AS importee "
                    "LIMIT $limit",
                    a=pair.importer, b=pair.importee,
                    limit=SAMPLE_LIMIT - len(detected), **scope_params,
                ))
                for r in rows:
                    detected.append({
                        "importer_package": pair.importer,
                        "importee_package": pair.importee,
                        "importer": r["importer"],
                        "importee": r["importee"],
                    })
    detail_pairs = ", ".join(f"{p.importer}→{p.importee}" for p in cfg.pairs)
    return PolicyResult(
        name="cross_package",
        passed=(total == 0),
        violation_count=total,
        sample=detected,
        detail=f"Forbidden import directions: {detail_pairs or '(none configured)'}.",
    )


def _check_layer_bypass(
    driver: Driver, cfg: LayerBypassConfig, scope: list[str] | None = None,
) -> PolicyResult:
    """Controllers reaching ``*Repository`` without traversing ``*Service``."""
    labels_or = "|".join(cfg.controller_labels)
    depth = f"*1..{cfg.call_depth}"
    scope_frag, scope_params = _scope_filter("ctrl", "file", scope)
    scope_clause = f"  AND {scope_frag}\n" if scope_frag else ""
    sample_cypher = (
        f"MATCH (ctrl:{labels_or})-[:HAS_METHOD]->(m:Method)"
        f"-[:CALLS{depth}]->(target:Method)\n"
        f"MATCH (repo:Class)-[:HAS_METHOD]->(target)\n"
        f"WHERE repo.name ENDS WITH $repo_suffix\n"
        f"{scope_clause}"
        f"  AND NOT EXISTS {{\n"
        f"    MATCH (ctrl)-[:HAS_METHOD]->(:Method)-[:CALLS{depth}]->(:Method)"
        f"<-[:HAS_METHOD]-(svc:Class)\n"
        f"    WHERE svc.name ENDS WITH $svc_suffix\n"
        f"  }}\n"
        f"RETURN DISTINCT ctrl.name AS controller, repo.name AS repository, "
        f"target.name AS method\n"
        f"ORDER BY ctrl.name, repo.name, target.name\n"
        f"LIMIT $limit"
    )
    count_cypher = (
        f"MATCH (ctrl:{labels_or})-[:HAS_METHOD]->(m:Method)"
        f"-[:CALLS{depth}]->(target:Method)\n"
        f"MATCH (repo:Class)-[:HAS_METHOD]->(target)\n"
        f"WHERE repo.name ENDS WITH $repo_suffix\n"
        f"{scope_clause}"
        f"  AND NOT EXISTS {{\n"
        f"    MATCH (ctrl)-[:HAS_METHOD]->(:Method)-[:CALLS{depth}]->(:Method)"
        f"<-[:HAS_METHOD]-(svc:Class)\n"
        f"    WHERE svc.name ENDS WITH $svc_suffix\n"
        f"  }}\n"
        f"RETURN count(DISTINCT ctrl) AS v"
    )
    with driver.session() as s:
        total = int(s.run(
            count_cypher,
            repo_suffix=cfg.repository_suffix,
            svc_suffix=cfg.service_suffix,
            **scope_params,
        ).single()["v"] or 0)
        sample = [dict(r) for r in s.run(
            sample_cypher,
            repo_suffix=cfg.repository_suffix,
            svc_suffix=cfg.service_suffix,
            limit=SAMPLE_LIMIT,
            **scope_params,
        )]
    return PolicyResult(
        name="layer_bypass",
        passed=(total == 0),
        violation_count=total,
        sample=sample,
        detail=(
            f"{'/'.join(cfg.controller_labels)} calling *{cfg.repository_suffix} "
            f"methods without a *{cfg.service_suffix} layer in between."
        ),
    )


def _check_coupling_ceiling(
    driver: Driver, cfg: CouplingCeilingConfig, scope: list[str] | None = None,
) -> PolicyResult:
    """Flag files with more than ``cfg.max_imports`` distinct file-level imports."""
    scope_frag, scope_params = _scope_filter("f", "path", scope)
    where = f"WHERE {scope_frag}\n" if scope_frag else ""
    count_cypher = (
        "MATCH (f:File)-[:IMPORTS]->(g:File)\n"
        f"{where}"
        "WITH f, count(g) AS deps\n"
        "WHERE deps > $threshold\n"
        "RETURN count(f) AS v"
    )
    sample_cypher = (
        "MATCH (f:File)-[:IMPORTS]->(g:File)\n"
        f"{where}"
        "WITH f, count(g) AS deps\n"
        "WHERE deps > $threshold\n"
        "RETURN f.path AS file, deps\n"
        "ORDER BY deps DESC\n"
        "LIMIT $limit"
    )
    with driver.session() as s:
        total = int(s.run(
            count_cypher, threshold=cfg.max_imports, **scope_params,
        ).single()["v"] or 0)
        sample = [dict(r) for r in s.run(
            sample_cypher, threshold=cfg.max_imports, limit=SAMPLE_LIMIT,
            **scope_params,
        )]
    return PolicyResult(
        name="coupling_ceiling",
        passed=(total == 0),
        violation_count=total,
        sample=sample,
        detail=f"Files with more than {cfg.max_imports} distinct file-level imports.",
    )


def _check_orphans(
    driver: Driver, cfg: OrphanDetectionConfig, scope: list[str] | None = None,
) -> PolicyResult:
    """Flag functions/classes/atoms/endpoints with zero inbound references."""
    # Build sub-queries for each requested kind.
    _kind_queries = {
        "function": (
            "MATCH (f:Function)\n"
            "WHERE NOT EXISTS { ()-[:CALLS]->(f) }\n"
            "  AND NOT EXISTS { ()-[:RENDERS]->(f) }\n"
            "  AND NOT EXISTS { (f)-[:DECORATED_BY]->(:Decorator) }\n"
            "  AND NOT f.name STARTS WITH 'test_'\n"
            "  AND NOT f.name IN ['setup_module', 'teardown_module',\n"
            "                     'setup_function', 'teardown_function',\n"
            "                     'setup_class', 'teardown_class',\n"
            "                     'setup_method', 'teardown_method']\n"
            "{prefix_filter}"
            "RETURN 'orphan_function' AS kind, f.name AS name, f.file AS file"
        ),
        "class": (
            "MATCH (c:Class)\n"
            "WHERE NOT EXISTS { ()-[:EXTENDS]->(c) }\n"
            "  AND NOT EXISTS { ()-[:INJECTS]->(c) }\n"
            "  AND NOT EXISTS { ()-[:RESOLVES]->(c) }\n"
            "  AND NOT EXISTS { (:File)-[:IMPORTS_SYMBOL {symbol: c.name}]->(:File) }\n"
            "{prefix_filter}"
            "RETURN 'orphan_class' AS kind, c.name AS name, c.file AS file"
        ),
        "atom": (
            "MATCH (a:Atom)\n"
            "WHERE NOT EXISTS { ()-[:READS_ATOM]->(a) }\n"
            "  AND NOT EXISTS { ()-[:WRITES_ATOM]->(a) }\n"
            "{prefix_filter}"
            "RETURN 'orphan_atom' AS kind, a.name AS name, a.file AS file"
        ),
        "endpoint": (
            "MATCH (e:Endpoint)\n"
            "WHERE NOT EXISTS { (:Method)-[:HANDLES]->(e) }\n"
            "{prefix_filter}"
            "RETURN 'orphan_endpoint' AS kind, "
            "(e.method + ' ' + e.path) AS name, e.file AS file"
        ),
    }
    # Variable used in the prefix filter differs per kind.
    _kind_var = {"function": "f", "class": "c", "atom": "a", "endpoint": "e"}

    # Determine the path filter: explicit path_prefix wins, then --scope,
    # then no filter.  scope_extra holds any params the scope filter needs.
    scope_extra: dict = {}
    parts: list[str] = []
    for kind in cfg.kinds:
        tmpl = _kind_queries[kind]
        if cfg.path_prefix:
            pf = f"  AND {_kind_var[kind]}.file STARTS WITH $prefix\n"
        elif scope:
            sf, sp = _scope_filter(_kind_var[kind], "file", scope)
            pf = f"  AND {sf}\n"
            scope_extra.update(sp)
        else:
            pf = ""
        parts.append(tmpl.replace("{prefix_filter}", pf))

    union = "\nUNION ALL\n".join(parts)
    count_cypher = f"CALL () {{\n{union}\n}}\nRETURN count(*) AS v"
    sample_cypher = (
        f"{union}\n"
        f"ORDER BY kind, file, name\n"
        f"LIMIT $limit"
    )

    params: dict = {"limit": SAMPLE_LIMIT}
    if cfg.path_prefix:
        params["prefix"] = cfg.path_prefix
    params.update(scope_extra)

    with driver.session() as s:
        total = int(s.run(count_cypher, **params).single()["v"] or 0)
        sample = [dict(r) for r in s.run(sample_cypher, **params)]

    kinds_str = ", ".join(cfg.kinds)
    return PolicyResult(
        name="orphan_detection",
        passed=(total == 0),
        violation_count=total,
        sample=sample,
        detail=f"Symbols with zero inbound references (kinds: {kinds_str}).",
    )


def _check_custom(driver: Driver, custom: CustomPolicy) -> PolicyResult:
    """Run a user-authored policy from :class:`CustomPolicy`."""
    with driver.session() as s:
        count_result = s.run(custom.count_cypher).single()
        total = int((count_result["v"] if count_result else 0) or 0)
        sample: list[dict] = []
        if total > 0:
            sample = [dict(r) for r in s.run(custom.sample_cypher)][:SAMPLE_LIMIT]
    return PolicyResult(
        name=custom.name,
        passed=(total == 0),
        violation_count=total,
        sample=sample,
        detail=custom.description or "(user-defined policy)",
    )


# ── Rendering ────────────────────────────────────────────────

def _render(console: Console, report: ArchReport) -> None:
    """Pretty-print an :class:`ArchReport` using Rich (mirrors validate._render)."""
    console.rule("[bold cyan]Architecture conformance")
    t = Table(show_header=True, header_style="bold magenta")
    t.add_column("result", width=6)
    t.add_column("policy")
    t.add_column("violations", justify="right")
    t.add_column("detail", style="dim")
    for p in report.policies:
        if "(disabled" in p.detail:
            mark = "[yellow]SKIP"
        elif p.passed:
            mark = "[green]PASS"
        else:
            mark = "[red]FAIL"
        t.add_row(mark, p.name, str(p.violation_count), p.detail)
    console.print(t)

    for p in report.policies:
        if p.passed or not p.sample:
            continue
        console.print(f"\n[bold red]{p.name}[/] — first {len(p.sample)} of {p.violation_count}")
        headers = list(p.sample[0].keys())
        tbl = Table(show_header=True, header_style="bold magenta")
        for h in headers:
            tbl.add_column(h)
        for row in p.sample:
            tbl.add_row(*[str(row.get(h, "")) for h in headers])
        console.print(tbl)

    passed = sum(1 for p in report.policies if p.passed)
    total = len(report.policies)
    style = "bold green" if passed == total else "bold red"
    console.print(f"\n[{style}]{passed}/{total} policies passed[/]")
