"""codegraph CLI: index, query, validate."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import typer
from neo4j import GraphDatabase
from rich.console import Console
from rich.table import Table

from .loader import Neo4jLoader
from .parser import TsParser
from .resolver import Index, Resolver, link_cross_file, load_package_config

app = typer.Typer(help="codegraph — map a TS/TSX codebase into Neo4j")
console = Console()


DEFAULT_URI = os.environ.get("CODEGRAPH_NEO4J_URI", "bolt://localhost:7688")
DEFAULT_USER = os.environ.get("CODEGRAPH_NEO4J_USER", "neo4j")
DEFAULT_PASS = os.environ.get("CODEGRAPH_NEO4J_PASS", "codegraph123")

DEFAULT_EXCLUDE_DIRS = {
    "node_modules", "dist", "build", ".next", ".turbo", "coverage",
    ".git", "generated", "__generated__", ".cache",
}
DEFAULT_EXCLUDE_SUFFIXES = (
    ".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx",
    ".stories.tsx", ".d.ts",
)


# ── index ────────────────────────────────────────────────────

@app.command()
def index(
    repo: Path = typer.Argument(..., help="Repo root to index", exists=True, file_okay=False),
    packages: list[str] = typer.Option(
        ["twenty-server", "twenty-front"],
        "--package", "-p",
        help="Package directory names under repo/packages/ to include",
    ),
    wipe: bool = typer.Option(True, help="Wipe Neo4j database before load"),
    uri: str = DEFAULT_URI,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASS,
    max_files: Optional[int] = typer.Option(None, help="Limit files (debug)"),
) -> None:
    """Parse all TS/TSX files in the selected packages and load into Neo4j."""
    repo = repo.resolve()
    console.print(f"[bold]Indexing[/] {repo}  packages={packages}")

    # 1. Discover files and build per-package configs
    parser = TsParser()
    index_obj = Index()

    pkg_configs = []
    for pkg_name in packages:
        pkg_dir = repo / "packages" / pkg_name
        if not pkg_dir.exists():
            console.print(f"[yellow]skip[/] package {pkg_name} (not found)")
            continue
        pkg_configs.append(load_package_config(repo, pkg_dir))
        console.print(
            f"  [green]•[/] {pkg_name}: aliases={list(pkg_configs[-1].aliases.keys())}"
        )

    # 2. Walk files
    to_parse: list[tuple[Path, str, str]] = []
    for pkg in pkg_configs:
        for p in pkg.root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in DEFAULT_EXCLUDE_DIRS for part in p.parts):
                continue
            if p.suffix.lower() not in (".ts", ".tsx"):
                continue
            name_lower = p.name.lower()
            if any(name_lower.endswith(suf) for suf in DEFAULT_EXCLUDE_SUFFIXES):
                continue
            try:
                if p.stat().st_size > 1_500_000:
                    continue
            except OSError:
                continue
            rel = str(p.resolve().relative_to(repo)).replace("\\", "/")
            to_parse.append((p, rel, pkg.name))
    if max_files is not None:
        to_parse = to_parse[:max_files]
    console.print(f"[bold]Parsing[/] {len(to_parse)} files…")

    # 3. Parse
    t0 = time.time()
    progress_step = max(1, len(to_parse) // 20)
    for i, (abs_p, rel, pkg_name) in enumerate(to_parse):
        result = parser.parse_file(abs_p, rel, pkg_name)
        if result is None:
            continue
        index_obj.add(result)
        if (i + 1) % progress_step == 0:
            console.print(f"  parsed {i+1}/{len(to_parse)}  [{time.time()-t0:.1f}s]")
    console.print(f"[bold green]✓[/] parsed {len(index_obj.files_by_path)} files in {time.time()-t0:.1f}s")

    # 4. Resolve cross-file references
    console.print("[bold]Resolving imports + references…")
    resolver = Resolver(repo, pkg_configs)
    t0 = time.time()
    all_edges = link_cross_file(index_obj, resolver)
    # Also include edges emitted directly during parsing (DECORATED_BY etc.)
    for r in index_obj.files_by_path.values():
        all_edges.extend(r.edges)
    stats = next((e for e in all_edges if e.kind == "__STATS__"), None)
    if stats:
        ti = stats.props.get("total_imports", 0)
        ui = stats.props.get("unresolved_imports", 0)
        pct = 100.0 * (ti - ui) / ti if ti else 0.0
        console.print(
            f"  imports: total={ti} resolved={ti-ui} unresolved={ui} "
            f"({pct:.1f}% resolved)  [{time.time()-t0:.1f}s]"
        )

    # 5. Neo4j load
    console.print("[bold]Connecting to Neo4j…", DEFAULT_URI if uri is DEFAULT_URI else uri)
    loader = Neo4jLoader(uri, user, password)
    try:
        loader.init_schema()
        if wipe:
            console.print("[yellow]Wiping database…")
            loader.wipe()
            loader.init_schema()
        t0 = time.time()
        ls = loader.load(index_obj, [e for e in all_edges if e.kind != "__STATS__"])
        console.print(f"[bold green]✓[/] loaded in {time.time()-t0:.1f}s")
        _print_load_stats(ls)
    finally:
        loader.close()


def _print_load_stats(stats) -> None:
    t = Table(title="Load stats", show_header=True, header_style="bold magenta")
    t.add_column("entity"); t.add_column("count", justify="right")
    for k in ("files", "classes", "functions", "interfaces", "endpoints", "externals"):
        t.add_row(k, str(getattr(stats, k)))
    for k, v in sorted(stats.edges.items()):
        t.add_row(f"edge:{k}", str(v))
    console.print(t)


# ── validate ─────────────────────────────────────────────────

@app.command()
def validate(
    repo: Path = typer.Argument(..., help="Repo root (for grep-based ground truth)", exists=True, file_okay=False),
    uri: str = DEFAULT_URI,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASS,
) -> None:
    """Run coverage metrics + ground-truth assertions + smoke queries."""
    from .validate import run_validation
    report = run_validation(uri, user, password, repo.resolve(), console)
    raise typer.Exit(code=0 if report.ok else 1)


# ── query ────────────────────────────────────────────────────

@app.command()
def query(
    cypher: str = typer.Argument(..., help="A Cypher query"),
    uri: str = DEFAULT_URI,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASS,
    limit: int = typer.Option(20, help="Row limit"),
) -> None:
    """Run an ad-hoc Cypher query and print results as a table."""
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as s:
            rows = list(s.run(cypher))[:limit]
    finally:
        driver.close()

    if not rows:
        console.print("[dim](no rows)[/]")
        return
    headers = list(rows[0].keys())
    t = Table(show_header=True, header_style="bold magenta")
    for h in headers:
        t.add_column(h)
    for r in rows:
        t.add_row(*[str(r.get(h, "")) for h in headers])
    console.print(t)


# ── wipe ─────────────────────────────────────────────────────

@app.command()
def wipe(
    uri: str = DEFAULT_URI,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASS,
) -> None:
    """Drop all nodes and edges from the Neo4j database."""
    loader = Neo4jLoader(uri, user, password)
    try:
        loader.wipe()
        console.print("[green]✓[/] wiped")
    finally:
        loader.close()


if __name__ == "__main__":
    app()
