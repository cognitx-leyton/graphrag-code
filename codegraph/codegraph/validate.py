"""Validation harness: coverage metrics + ground-truth assertions + smoke queries."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from neo4j import Driver, GraphDatabase
from rich.console import Console
from rich.table import Table


TOL = 0.05  # ±5% tolerance on cross-checks


@dataclass
class AssertionResult:
    name: str
    passed: bool
    expected: str
    actual: str
    detail: str = ""


@dataclass
class ValidationReport:
    coverage: dict[str, float]
    assertions: list[AssertionResult]
    smoke: list[tuple[str, list[dict]]]

    @property
    def ok(self) -> bool:
        return all(a.passed for a in self.assertions)


# ── Entry point ──────────────────────────────────────────────

def run_validation(
    uri: str,
    user: str,
    password: str,
    repo_root: Path,
    console: Optional[Console] = None,
) -> ValidationReport:
    console = console or Console()
    driver = GraphDatabase.driver(uri, auth=(user, password))

    coverage = _coverage_metrics(driver)
    assertions = _ground_truth_assertions(driver, repo_root)
    smoke = _smoke_queries(driver)
    driver.close()

    _render(console, coverage, assertions, smoke)
    return ValidationReport(coverage=coverage, assertions=assertions, smoke=smoke)


# ── Coverage metrics ─────────────────────────────────────────

def _coverage_metrics(driver: Driver) -> dict[str, float]:
    q = {
        "files_total": "MATCH (f:File) RETURN count(f) AS v",
        "files_ts": "MATCH (f:File {language:'ts'}) RETURN count(f) AS v",
        "files_tsx": "MATCH (f:File {language:'tsx'}) RETURN count(f) AS v",
        "classes": "MATCH (c:Class) RETURN count(c) AS v",
        "functions": "MATCH (f:Function) RETURN count(f) AS v",
        "components": "MATCH (f:Function {is_component:true}) RETURN count(f) AS v",
        "interfaces": "MATCH (i:Interface) RETURN count(i) AS v",
        "endpoints": "MATCH (e:Endpoint) RETURN count(e) AS v",
        "externals": "MATCH (x:External) RETURN count(x) AS v",
        "imports_resolved": "MATCH ()-[r:IMPORTS]->() RETURN count(r) AS v",
        "imports_external": "MATCH ()-[r:IMPORTS_EXTERNAL]->() RETURN count(r) AS v",
        "injects": "MATCH ()-[r:INJECTS]->() RETURN count(r) AS v",
        "extends": "MATCH ()-[r:EXTENDS]->() RETURN count(r) AS v",
        "renders": "MATCH ()-[r:RENDERS]->() RETURN count(r) AS v",
        "uses_hook": "MATCH ()-[r:USES_HOOK]->() RETURN count(r) AS v",
        "orphan_files": (
            "MATCH (f:File) "
            "WHERE NOT (f)-[:IMPORTS|IMPORTS_EXTERNAL]->() "
            "  AND NOT ()-[:IMPORTS]->(f) "
            "RETURN count(f) AS v"
        ),
    }
    out: dict[str, float] = {}
    with driver.session() as s:
        for k, cypher in q.items():
            rec = s.run(cypher).single()
            out[k] = float(rec["v"]) if rec else 0.0

    total = out["imports_resolved"] + out["imports_external"]
    out["import_resolution_pct"] = (
        100.0 * out["imports_resolved"] / total if total > 0 else 0.0
    )
    return out


# ── Ground-truth assertions ──────────────────────────────────

def _ground_truth_assertions(driver: Driver, repo_root: Path) -> list[AssertionResult]:
    results: list[AssertionResult] = []

    def assert_count(name: str, cypher: str, expected: int, tol: float = TOL) -> None:
        with driver.session() as s:
            rec = s.run(cypher).single()
            actual = int(rec["v"]) if rec else 0
        low, high = int(expected * (1 - tol)), int(expected * (1 + tol) + 0.999)
        passed = low <= actual <= high
        results.append(AssertionResult(
            name=name,
            passed=passed,
            expected=f"~{expected} (±{int(tol*100)}%)",
            actual=str(actual),
            detail=f"range [{low}, {high}]",
        ))

    def assert_exact(name: str, cypher: str, expected, detail: str = "") -> None:
        with driver.session() as s:
            rec = s.run(cypher).single()
            actual = rec["v"] if rec else None
        passed = str(actual) == str(expected)
        results.append(AssertionResult(
            name=name,
            passed=passed,
            expected=str(expected),
            actual=str(actual),
            detail=detail,
        ))

    def assert_true(name: str, cypher: str, detail: str = "") -> None:
        with driver.session() as s:
            rec = s.run(cypher).single()
            val = rec["v"] if rec else None
        passed = bool(val)
        results.append(AssertionResult(
            name=name,
            passed=passed,
            expected="truthy",
            actual=str(val),
            detail=detail,
        ))

    # --- Parser structural counts (cross-checked against grep on source) ---
    # Ground truth derived from easy-builder/packages/twenty-server/src:
    #   @Controller classes: ~35
    #   HTTP endpoint decorators: ~110
    #   @Injectable classes: ~900
    #   @Module classes: ~292

    gt = _compute_source_ground_truth(repo_root)

    assert_count(
        "controllers match grep",
        "MATCH (c:Class {is_controller:true}) RETURN count(c) AS v",
        gt["controllers"],
    )
    assert_count(
        "endpoints match grep",
        "MATCH (e:Endpoint) RETURN count(e) AS v",
        gt["endpoints"],
    )
    assert_count(
        "injectable classes match grep",
        "MATCH (c:Class {is_injectable:true}) RETURN count(c) AS v",
        gt["injectables"],
    )
    assert_count(
        "module classes match grep",
        "MATCH (c:Class {is_module:true}) RETURN count(c) AS v",
        gt["modules"],
    )

    # --- Specific files / nodes ---
    assert_true(
        "google-auth.controller file exists",
        """
        MATCH (f:File)
        WHERE f.path ENDS WITH 'google-auth.controller.ts'
        RETURN count(f) > 0 AS v
        """,
    )

    assert_exact(
        "GoogleAuthController has exactly 2 endpoints",
        """
        MATCH (f:File)-[:DEFINES_CLASS]->(c:Class)-[:EXPOSES]->(e:Endpoint)
        WHERE f.path ENDS WITH 'google-auth.controller.ts'
        RETURN count(e) AS v
        """,
        2,
        detail="Source has @Get() and @Get('redirect')",
    )

    assert_true(
        "GoogleAuthController endpoint paths include '/auth/google'",
        """
        MATCH (f:File)-[:DEFINES_CLASS]->(c:Class)-[:EXPOSES]->(e:Endpoint)
        WHERE f.path ENDS WITH 'google-auth.controller.ts'
          AND e.path CONTAINS '/auth/google'
        RETURN count(e) > 0 AS v
        """,
    )

    assert_true(
        "GoogleAuthController INJECTS AuthService",
        """
        MATCH (c:Class {name:'GoogleAuthController'})-[:INJECTS]->(s:Class {name:'AuthService'})
        RETURN count(s) > 0 AS v
        """,
    )

    # --- Structural sanity ---
    assert_true(
        "import resolution ≥ 60%",
        """
        MATCH ()-[r:IMPORTS]->() WITH count(r) AS ok
        MATCH ()-[x:IMPORTS_EXTERNAL]->() WITH ok, count(x) AS ext
        RETURN 1.0 * ok / (ok + ext) >= 0.60 AS v
        """,
        detail="relative+alias imports should mostly resolve",
    )

    assert_true(
        "every controller has ≥ 1 endpoint",
        """
        MATCH (c:Class {is_controller:true})
        OPTIONAL MATCH (c)-[:EXPOSES]->(e:Endpoint)
        WITH c, count(e) AS n
        WITH collect(n) AS counts
        RETURN all(x IN counts WHERE x >= 1) AS v
        """,
    )

    # twenty-front expectations
    assert_true(
        "some React components exist",
        """
        MATCH (f:Function {is_component:true})
        RETURN count(f) > 100 AS v
        """,
    )

    assert_true(
        "RENDERS edges exist",
        "MATCH ()-[r:RENDERS]->() RETURN count(r) > 50 AS v",
    )

    assert_true(
        "USES_HOOK edges include useState/useEffect",
        """
        MATCH (h:Hook) WHERE h.name IN ['useState','useEffect']
        RETURN count(h) = 2 AS v
        """,
    )

    # --- Decorator node presence ---
    assert_true(
        "decorator catalog contains NestJS staples",
        """
        MATCH (d:Decorator)
        WHERE d.name IN ['Controller','Injectable','Module','Get','Post']
        RETURN count(DISTINCT d.name) >= 5 AS v
        """,
    )

    return results


def _compute_source_ground_truth(repo_root: Path) -> dict[str, int]:
    """Run `grep` over the real source to get expected counts."""
    server_src = repo_root / "packages" / "twenty-server" / "src"

    def grep_count(pattern: str) -> int:
        try:
            out = subprocess.run(
                ["grep", "-rhE", pattern, str(server_src), "--include=*.ts"],
                capture_output=True, text=True, check=False,
            )
            return len([ln for ln in out.stdout.splitlines() if ln.strip()])
        except Exception:
            return 0

    return {
        "controllers": grep_count(r"^@Controller\b"),
        "endpoints": grep_count(r"^\s*@(Get|Post|Put|Patch|Delete|Options|Head|All)\s*\("),
        "injectables": grep_count(r"^@Injectable\s*\("),
        "modules": grep_count(r"^@Module\s*\("),
    }


# ── Smoke queries ────────────────────────────────────────────

def _smoke_queries(driver: Driver) -> list[tuple[str, list[dict]]]:
    queries = [
        ("Top 10 controllers by endpoint count", """
            MATCH (c:Class {is_controller:true})-[:EXPOSES]->(e:Endpoint)
            RETURN c.name AS controller, c.base_path AS base, count(e) AS endpoints
            ORDER BY endpoints DESC LIMIT 10
        """),
        ("Top 10 most-injected services", """
            MATCH (s:Class {is_injectable:true})<-[:INJECTS]-()
            RETURN s.name AS service, count(*) AS injections
            ORDER BY injections DESC LIMIT 10
        """),
        ("Hub files (most incoming imports)", """
            MATCH (f:File)<-[:IMPORTS]-(other:File)
            RETURN f.path AS path, count(other) AS in_imports
            ORDER BY in_imports DESC LIMIT 10
        """),
        ("Top 10 hooks by usage", """
            MATCH (h:Hook)<-[:USES_HOOK]-()
            RETURN h.name AS hook, count(*) AS uses
            ORDER BY uses DESC LIMIT 10
        """),
        ("Top 10 rendered components", """
            MATCH (c:Function {is_component:true})<-[:RENDERS]-()
            RETURN c.name AS component, count(*) AS renders
            ORDER BY renders DESC LIMIT 10
        """),
        ("Sample endpoints", """
            MATCH (c:Class)-[:EXPOSES]->(e:Endpoint)
            RETURN c.name AS controller, e.method AS method, e.path AS path
            ORDER BY c.name LIMIT 15
        """),
    ]
    results = []
    with driver.session() as s:
        for title, q in queries:
            try:
                rows = [dict(r) for r in s.run(q)]
            except Exception as exc:
                rows = [{"error": str(exc)}]
            results.append((title, rows))
    return results


# ── Reporting ────────────────────────────────────────────────

def _render(console: Console, coverage: dict, assertions: list[AssertionResult], smoke: list) -> None:
    console.rule("[bold cyan]Coverage metrics")
    t = Table(show_header=True, header_style="bold magenta")
    t.add_column("metric"); t.add_column("value", justify="right")
    for k in [
        "files_total", "files_ts", "files_tsx",
        "classes", "functions", "components", "interfaces",
        "endpoints", "externals",
        "imports_resolved", "imports_external", "import_resolution_pct",
        "injects", "extends", "renders", "uses_hook", "orphan_files",
    ]:
        v = coverage.get(k, 0.0)
        if k == "import_resolution_pct":
            s = f"{v:.1f}%"
        else:
            s = f"{int(v)}"
        t.add_row(k, s)
    console.print(t)

    console.rule("[bold cyan]Ground-truth assertions")
    t = Table(show_header=True, header_style="bold magenta")
    t.add_column("result", width=6)
    t.add_column("name")
    t.add_column("expected")
    t.add_column("actual")
    t.add_column("detail", style="dim")
    for a in assertions:
        mark = "[green]PASS" if a.passed else "[red]FAIL"
        t.add_row(mark, a.name, a.expected, a.actual, a.detail)
    console.print(t)
    total = len(assertions)
    passed = sum(1 for a in assertions if a.passed)
    style = "bold green" if passed == total else "bold yellow"
    console.print(f"[{style}]{passed}/{total} assertions passed[/]")

    console.rule("[bold cyan]Smoke queries")
    for title, rows in smoke:
        console.print(f"\n[bold]{title}[/]")
        if not rows:
            console.print("  [dim](no rows)[/]")
            continue
        headers = list(rows[0].keys())
        t = Table(show_header=True, header_style="bold magenta")
        for h in headers:
            t.add_column(h)
        for r in rows:
            t.add_row(*[str(r.get(h, "")) for h in headers])
        console.print(t)
