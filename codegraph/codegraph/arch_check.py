"""Architecture-conformance policies.

Runs a fixed set of policies as Cypher against the live Neo4j graph and
returns an :class:`ArchReport`. Mirrors :mod:`codegraph.validate`'s shape:
typed result dataclasses, Rich-table rendering when a console is attached,
JSON serialisation for CI, and a ``ok`` rollup that maps directly to a
process exit code.

Built-in policies (v1):

- **import_cycles** — file IMPORTS cycles of length 2-6.
- **cross_package** — `twenty-front` files importing from `twenty-server`
  (a hard architectural boundary). Trivially generalisable by editing the
  tuple pairs in :data:`CROSS_PACKAGE_PAIRS`.
- **layer_bypass** — controllers that reach a ``*Repository`` method within
  3 hops without traversing a ``*Service``.

Extension via a repo-local ``.arch-policies.toml`` is a Stage 2 decision —
for v1 the list is hardcoded here.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

from neo4j import Driver, GraphDatabase
from rich.console import Console
from rich.table import Table


# ── Built-in policy definitions ──────────────────────────────

# Cross-package boundary rules: (importer_package, importee_package).
# Extend this list (or fork the module) to encode additional forbidden
# import directions for your repo. Each tuple becomes one violation bucket.
CROSS_PACKAGE_PAIRS: list[tuple[str, str]] = [
    ("twenty-front", "twenty-server"),
]

# Sample-size cap per policy — keeps the report skimmable.
SAMPLE_LIMIT = 10


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
) -> ArchReport:
    """Open a driver, evaluate every built-in policy, return an :class:`ArchReport`.

    The driver lifecycle mirrors :func:`codegraph.validate.run_validation`:
    open the driver, run every policy in its own session, close on the way out
    regardless of outcome. ``console`` is optional — pass ``None`` to suppress
    rendering (useful for ``--json`` mode).
    """
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        policies = [
            _check_import_cycles(driver),
            _check_cross_package(driver),
            _check_layer_bypass(driver),
        ]
    finally:
        driver.close()

    report = ArchReport(policies=policies)
    if console is not None:
        _render(console, report)
    return report


# ── Policies ─────────────────────────────────────────────────

def _check_import_cycles(driver: Driver) -> PolicyResult:
    """Detect file-level IMPORTS cycles of length 2-6."""
    cypher = """
    MATCH path = (a:File)-[:IMPORTS*2..6]->(a)
    WITH [n IN nodes(path) | n.path] AS cycle, length(path) AS hops
    RETURN DISTINCT cycle, hops
    ORDER BY hops ASC, cycle[0]
    LIMIT $limit
    """
    count_cypher = """
    MATCH path = (a:File)-[:IMPORTS*2..6]->(a)
    RETURN count(DISTINCT path) AS v
    """
    with driver.session() as s:
        total = int(s.run(count_cypher).single()["v"] or 0)
        sample = [dict(r) for r in s.run(cypher, limit=SAMPLE_LIMIT)]
    return PolicyResult(
        name="import_cycles",
        passed=(total == 0),
        violation_count=total,
        sample=sample,
        detail="Files (or packages) that import each other transitively.",
    )


def _check_cross_package(driver: Driver) -> PolicyResult:
    """Detect imports that cross a forbidden package boundary.

    Forbidden pairs live in :data:`CROSS_PACKAGE_PAIRS`. A single policy
    result aggregates violations across every pair so the CI output stays
    compact.
    """
    detected: list[dict] = []
    total = 0
    with driver.session() as s:
        for importer_pkg, importee_pkg in CROSS_PACKAGE_PAIRS:
            count = int(s.run(
                "MATCH (a:File)-[:IMPORTS]->(b:File) "
                "WHERE a.package = $a AND b.package = $b "
                "RETURN count(*) AS v",
                a=importer_pkg, b=importee_pkg,
            ).single()["v"] or 0)
            total += count
            if count and len(detected) < SAMPLE_LIMIT:
                rows = list(s.run(
                    "MATCH (a:File)-[:IMPORTS]->(b:File) "
                    "WHERE a.package = $a AND b.package = $b "
                    "RETURN a.path AS importer, b.path AS importee "
                    "LIMIT $limit",
                    a=importer_pkg, b=importee_pkg,
                    limit=SAMPLE_LIMIT - len(detected),
                ))
                for r in rows:
                    detected.append({
                        "importer_package": importer_pkg,
                        "importee_package": importee_pkg,
                        "importer": r["importer"],
                        "importee": r["importee"],
                    })
    return PolicyResult(
        name="cross_package",
        passed=(total == 0),
        violation_count=total,
        sample=detected,
        detail=(
            f"Forbidden import directions: "
            f"{', '.join(f'{a}→{b}' for a, b in CROSS_PACKAGE_PAIRS)}."
        ),
    )


def _check_layer_bypass(driver: Driver) -> PolicyResult:
    """Controllers that reach a ``*Repository`` method without going through ``*Service``.

    Heuristic: name-based (classes ending in ``Repository`` / ``Service``).
    Projects using different conventions should fork this module and rename.
    """
    cypher = """
    MATCH (ctrl:Controller)-[:HAS_METHOD]->(m:Method)-[:CALLS*1..3]->(target:Method)
    MATCH (repo:Class)-[:HAS_METHOD]->(target)
    WHERE repo.name ENDS WITH 'Repository'
      AND NOT EXISTS {
        MATCH (ctrl)-[:HAS_METHOD]->(:Method)-[:CALLS*1..3]->(:Method)<-[:HAS_METHOD]-(svc:Class)
        WHERE svc.name ENDS WITH 'Service'
      }
    RETURN DISTINCT ctrl.name AS controller, repo.name AS repository, target.name AS method
    ORDER BY ctrl.name, repo.name, target.name
    LIMIT $limit
    """
    count_cypher = """
    MATCH (ctrl:Controller)-[:HAS_METHOD]->(m:Method)-[:CALLS*1..3]->(target:Method)
    MATCH (repo:Class)-[:HAS_METHOD]->(target)
    WHERE repo.name ENDS WITH 'Repository'
      AND NOT EXISTS {
        MATCH (ctrl)-[:HAS_METHOD]->(:Method)-[:CALLS*1..3]->(:Method)<-[:HAS_METHOD]-(svc:Class)
        WHERE svc.name ENDS WITH 'Service'
      }
    RETURN count(DISTINCT ctrl) AS v
    """
    with driver.session() as s:
        total = int(s.run(count_cypher).single()["v"] or 0)
        sample = [dict(r) for r in s.run(cypher, limit=SAMPLE_LIMIT)]
    return PolicyResult(
        name="layer_bypass",
        passed=(total == 0),
        violation_count=total,
        sample=sample,
        detail="Controllers calling Repository methods without a Service layer in between.",
    )


# ── Rendering ────────────────────────────────────────────────

def _render(console: Console, report: ArchReport) -> None:
    """Pretty-print an :class:`ArchReport` using Rich.

    Matches the rendering idiom from :func:`codegraph.validate._render` — one
    summary table up top, then per-policy details below. Green ``PASS`` /
    red ``FAIL`` markers + a final rollup line.
    """
    console.rule("[bold cyan]Architecture conformance")
    t = Table(show_header=True, header_style="bold magenta")
    t.add_column("result", width=6)
    t.add_column("policy")
    t.add_column("violations", justify="right")
    t.add_column("detail", style="dim")
    for p in report.policies:
        mark = "[green]PASS" if p.passed else "[red]FAIL"
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
