# Plan: `codegraph arch-check` CLI + GitHub Actions gate

## Summary

Promote `/arch-check` (currently a markdown slash command over `/graph`) into a first-class CLI subcommand `codegraph arch-check` that runs the 3 built-in architecture policies as Cypher against a live Neo4j graph, returns a typed `ArchReport`, and exits non-zero on violations. Wire it into a GitHub Actions workflow that spins up Neo4j as a service container, re-indexes the repo, and runs the check — failing the build when drift sneaks in. Mirror `validate.py`'s existing `AssertionResult` / `ValidationReport` pattern so the architecture shape feels native.

## User Story

As a maintainer of a codegraph-indexed repo,
I want arch-check to run in CI and block PRs that introduce import cycles, cross-package violations, or layer-bypasses,
so that architectural drift can't land silently and the codebase stays aligned with its design on its own.

## Metadata

| Field | Value |
|-------|-------|
| Type | NEW_CAPABILITY (CLI subcommand + CI gate) |
| Complexity | MEDIUM |
| Systems Affected | `codegraph/codegraph/`, `.github/workflows/`, `codegraph/tests/`, `CLAUDE.md`, `.claude/commands/arch-check.md` |
| New Dependencies | None — uses existing `neo4j`, `typer`, `rich` deps already in `pyproject.toml` |
| Generalized | Specific: targets arch-check; follows `validate.py`'s pattern for shape reuse |
| Depends On | `glimmering-painting-yao.md` (parser/loader fixes shipped), `graph-slash-commands.plan.md` (Cypher policies already drafted in `.claude/commands/arch-check.md`) |

## User answers locked in

- **Neo4j in CI**: service container in the workflow (single source of truth — same Cypher runs interactively and in CI).
- **Trigger configurability**: workflow ships with sensible defaults (PR to main), but the `on:` block is documented in the workflow YAML + CLAUDE.md so the repo owner can add `push` triggers trivially. GitHub Actions is already declarative; no config layer needed.
- **Policy set for v1**: 3 built-ins only (cycles / cross-package / Controller→Repo bypass). Extension via `.arch-policies.toml` is v2.

---

## Reuse Inventory

| What Exists | File:Line | How This Plan Reuses It |
|-------------|-----------|--------------------------|
| `@app.command()` Typer pattern | `codegraph/codegraph/cli.py:77,90,435,471,509` | Add `arch_check` as a 6th command, identical decorator shape |
| `validate.py` module structure | `codegraph/codegraph/validate.py:1-80` | Mirror the `AssertionResult` + `ValidationReport` dataclasses → rename to `PolicyResult` / `ArchReport` |
| Rich Console + Table rendering | `codegraph/codegraph/validate.py:44` (`_render`) | Reuse rendering idiom for the arch-check output |
| Neo4j `GraphDatabase.driver(...)` + session pattern | `codegraph/codegraph/validate.py:37-43` | Exact same driver lifecycle (open → run queries → close) |
| Cypher policies | `.claude/commands/arch-check.md:37-68` | Copy the 3 queries into Python string constants — no re-derivation |
| `DEFAULT_URI`, `DEFAULT_USER`, `DEFAULT_PASS` env-aware constants | `codegraph/codegraph/cli.py:50-52` | Same connection config for the subcommand |
| `--json` output flag pattern | `codegraph/codegraph/cli.py` (existing on `query`, `validate`) | Match for CI-parseable output |

---

## Patterns to Follow

### Typer subcommand with --json + exit codes (mirror `validate`)
```python
# SOURCE: codegraph/codegraph/cli.py (the validate command)
@app.command()
def arch_check(
    uri: str = DEFAULT_URI,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASS,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON to stdout"),
) -> None:
    """Run architecture-conformance policies against the live graph."""
    from .arch_check import run_arch_check
    report = run_arch_check(uri, user, password, console=(None if json_output else console))
    if json_output:
        typer.echo(report.to_json())
    raise typer.Exit(code=0 if report.ok else 1)
```

### Report dataclass (mirror `validate.py:17-34`)
```python
@dataclass
class PolicyResult:
    name: str           # "import_cycles" | "cross_package" | "layer_bypass"
    passed: bool
    violation_count: int
    sample: list[dict]  # Up to 10 representative violations
    detail: str = ""

@dataclass
class ArchReport:
    policies: list[PolicyResult]

    @property
    def ok(self) -> bool:
        return all(p.passed for p in self.policies)
```

### Live Cypher via existing driver lifecycle
```python
# Adapted from codegraph/codegraph/validate.py:37-45
def run_arch_check(uri, user, password, console=None) -> ArchReport:
    console = console or Console()
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
    _render(console, report)
    return report
```

### GitHub Actions service container (Neo4j)
```yaml
# .github/workflows/arch-check.yml
services:
  neo4j:
    image: neo4j:5-community
    env:
      NEO4J_AUTH: neo4j/codegraph123
    ports:
      - 7687:7687
    options: >-
      --health-cmd "wget -q --spider http://localhost:7474 || exit 1"
      --health-interval 10s
      --health-timeout 5s
      --health-retries 20
```

### Test a policy against a synthetic graph
```python
# Mirror codegraph/tests/test_py_resolver.py pattern — build tmp_path repo,
# index into a scratch Neo4j database, assert violation counts.
def test_cycle_policy_detects_two_file_cycle(tmp_path, neo4j_session):
    # write a→b and b→a imports, index, run policy, expect violation_count >= 1
```

---

## Files to Change

| File | Action | Purpose |
|------|--------|---------|
| `codegraph/codegraph/arch_check.py` | CREATE | `PolicyResult` / `ArchReport` + 3 policy functions + `run_arch_check` + `_render` |
| `codegraph/codegraph/cli.py` | UPDATE | Add `arch_check` `@app.command()` with `--json`, delegating to `arch_check.run_arch_check` |
| `.github/workflows/arch-check.yml` | CREATE | GitHub Actions: Neo4j service, checkout, install codegraph, index, run arch-check |
| `codegraph/tests/test_arch_check.py` | CREATE | Synthetic graphs: clean repo → 0 violations; bad repo → expected violation counts per policy |
| `.claude/commands/arch-check.md` | UPDATE | Point at the CLI as the source of truth; the slash command stays for interactive use |
| `CLAUDE.md` | UPDATE | New "Architecture drift" subsection explaining: CI runs arch-check; how to configure triggers; how to debug a failing check locally |
| `codegraph/docs/arch-policies.md` | CREATE | Policy reference: what each detects, why it matters, how to interpret violations (feeds into CI failure messages) |

**No schema changes. No new dependencies.**

---

## Tasks

### Task 1: Extract policies into `arch_check.py`

- **File**: `codegraph/codegraph/arch_check.py`
- **Action**: CREATE
- **Why not UPDATE?**: Separation of concerns — `validate.py` is for coverage/ground-truth; policies are a distinct concern. Adding to `validate.py` would bloat a cohesive module.
- **Read first**: `codegraph/codegraph/validate.py:1-80` (full structural pattern); `.claude/commands/arch-check.md:37-68` (the 3 Cypher policies to port).
- **Implement**:
  1. `PolicyResult` + `ArchReport` dataclasses (shape above).
  2. Three private policy functions `_check_import_cycles(driver)`, `_check_cross_package(driver)`, `_check_layer_bypass(driver)`. Each runs its Cypher, returns a `PolicyResult` with up to 10 sample violations, `passed = (violation_count == 0)`.
  3. `run_arch_check(uri, user, password, console=None) -> ArchReport` — orchestrates driver lifecycle + Rich table rendering.
  4. `_render(console, report)` — mirrors `validate._render`: one Rich table with policy name, pass/fail, violation count, first-sample summary.
  5. `ArchReport.to_json()` — serialize for `--json` flag.
- **Mirror**: `codegraph/codegraph/validate.py:37-45` (driver lifecycle) + `codegraph/codegraph/validate.py:17-34` (dataclass shape).
- **Validate**: `python -c "from codegraph.arch_check import run_arch_check; print('ok')"` imports cleanly.

### Task 2: Wire `arch_check` CLI subcommand

- **File**: `codegraph/codegraph/cli.py`
- **Action**: UPDATE
- **Read first**: `codegraph/codegraph/cli.py:471-509` (the existing `validate` command — copy its ergonomic shape including the `--json` flag).
- **Implement**:
  1. Import `run_arch_check` lazily inside the function body (matches existing pattern for other Neo4j-touching commands).
  2. Add `@app.command()` decorated function `arch_check(...)` with `uri`, `user`, `password`, `--json` flag.
  3. Run the check, render to console (Rich table when stdout is interactive; suppressed when `--json`).
  4. `raise typer.Exit(code=0 if report.ok else 1)`.
  5. Update the Typer app help string to include the new command.
- **Validate**: `codegraph/.venv/bin/codegraph --help` shows `arch-check`; `codegraph arch-check --help` prints the new command's options; `codegraph arch-check` returns exit code 0 on a clean graph.

### Task 3: Unit-test each policy against synthetic graphs

- **File**: `codegraph/tests/test_arch_check.py`
- **Action**: CREATE
- **Read first**: `codegraph/tests/test_py_resolver.py:36-52` (the `_run_pipeline` helper that parses tmp_path and builds an `Index`); `codegraph/tests/test_loader_partitioning.py:1-50` (monkeypatched `_run` pattern — likely reused for policy tests that don't need real Neo4j).
- **Implement**: 6 tests (2 per policy):
  - Cycle policy: synthetic `a.py -> b.py -> a.py` → assert `violation_count >= 1`; clean repo → `passed is True`.
  - Cross-package: build 2 package dirs `front/` + `server/`, one import across → assert violation; no cross-import → passed.
  - Layer bypass: build `Controller` class with `HAS_METHOD` method that `CALLS` a `*Repository` method without going through a `*Service` → assert violation; route via Service → passed.
- **Test strategy**: Since the policies require a live Neo4j session for their current implementation, use one of:
  - **(a)** Spin up the test Neo4j container (present in `codegraph/docker-compose.yml`) — matches CI exactly.
  - **(b)** Refactor each policy to accept a `Session` parameter so tests can pass a mock with canned `session.run(...)` responses.
  - **Decision**: **(a)** — already the dev convention for this repo; keeps Cypher as source of truth.
- **Validate**: `.venv/bin/python -m pytest tests/test_arch_check.py -q` — 6 passed, zero warnings. Full suite stays at 166+ green.

### Task 4: Ship the GitHub Actions workflow

- **File**: `.github/workflows/arch-check.yml`
- **Action**: CREATE
- **Read first**: `codegraph/docker-compose.yml` (the existing Neo4j image + port + password used locally; mirror in the workflow).
- **Implement**:
  ```yaml
  name: arch-check
  on:
    # Default: block on PR to main. Uncomment / extend as needed.
    pull_request:
      branches: [main]
    # push:
    #   branches: [dev]
    # workflow_dispatch:  # manual trigger for debugging
  jobs:
    arch-check:
      runs-on: ubuntu-latest
      services:
        neo4j:
          image: neo4j:5-community
          env:
            NEO4J_AUTH: neo4j/codegraph123
          ports: ["7687:7687", "7474:7474"]
          options: >-
            --health-cmd "wget -q --spider http://localhost:7474 || exit 1"
            --health-interval 10s
            --health-retries 20
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with:
            python-version: "3.12"
        - run: pip install -e "./codegraph[python]"
        - run: codegraph index . -p codegraph/codegraph -p codegraph/tests --no-wipe --skip-ownership
          env:
            CODEGRAPH_NEO4J_URI: bolt://localhost:7687
        - run: codegraph arch-check --json > arch-report.json
          env:
            CODEGRAPH_NEO4J_URI: bolt://localhost:7687
        - if: always()
          uses: actions/upload-artifact@v4
          with:
            name: arch-report
            path: arch-report.json
  ```
- **Configurability doc**: the `on:` block top of the file has 3 lines of commented-out alternatives (push-to-dev, workflow_dispatch, any-PR). The header comment explains each.
- **Validate**: `actionlint` (if installed) parses cleanly. Push a branch with a deliberate cycle to confirm the gate fires; push a clean branch to confirm it passes.

### Task 5: Update `.claude/commands/arch-check.md`

- **File**: `.claude/commands/arch-check.md`
- **Action**: UPDATE
- **Read first**: the current file.
- **Implement**: Add a new "## CI integration" subsection noting that `codegraph arch-check` (the CLI) is the authoritative runner, and CI runs the same Cypher. The slash command remains for interactive local use.

### Task 6: Update `CLAUDE.md`

- **File**: `CLAUDE.md`
- **Action**: UPDATE
- **Implement**: Add a new subsection "### Architecture drift (CI gate)" after the daily power-tool commands section. Explain:
  - PRs to main run `arch-check` via `.github/workflows/arch-check.yml`.
  - Failing policies block the PR.
  - How to reproduce locally: `codegraph index . && codegraph arch-check`.
  - How to change trigger scope: edit `on:` in the workflow YAML.
  - Link to `codegraph/docs/arch-policies.md` for policy reference.

### Task 7: Author the policy reference

- **File**: `codegraph/docs/arch-policies.md`
- **Action**: CREATE
- **Implement**: One section per policy. For each: what it detects (plain English), why it matters (concrete pain), the Cypher (copied from the command), how to interpret a violation, common false-positive patterns, how to refine/override. Link from CI failure messages ("arch-check failed — see docs/arch-policies.md").

### Task V1: Validate end-to-end

- **File**: none (runtime check)
- **Action**: VERIFY
- **Implement**:
  1. `pytest tests/ -q` — all tests pass, zero warnings.
  2. `codegraph arch-check` (against the live local graph) — exit 0, Rich table prints 3 green rows.
  3. `codegraph arch-check --json` — valid JSON on stdout.
  4. Push a test branch with a synthetic import cycle, confirm CI fails the PR.
  5. Merge this plan's PR with the cycle reverted, confirm CI passes.

---

## Validation

```bash
# Type-lite compile
codegraph/.venv/bin/python -m compileall codegraph/codegraph/arch_check.py

# Tests
cd codegraph && .venv/bin/python -m pytest tests/ -q

# CLI smoke
codegraph/.venv/bin/codegraph arch-check --help
codegraph/.venv/bin/codegraph arch-check
codegraph/.venv/bin/codegraph arch-check --json | jq '.policies | length'  # should be 3

# Workflow syntax (if actionlint installed)
actionlint .github/workflows/arch-check.yml
```

---

## Critical Files to Modify

- `codegraph/codegraph/arch_check.py` (new)
- `codegraph/codegraph/cli.py` (append one `@app.command()`)
- `codegraph/tests/test_arch_check.py` (new)
- `.github/workflows/arch-check.yml` (new)
- `.claude/commands/arch-check.md` (add CI section)
- `CLAUDE.md` (add "Architecture drift" subsection)
- `codegraph/docs/arch-policies.md` (new)

## Acceptance Criteria

- [ ] `codegraph arch-check` exits 0 on a clean graph and 1 on a graph with a violation
- [ ] `codegraph arch-check --json` emits a valid `ArchReport` JSON shape
- [ ] 6 new tests pass, zero warnings, total suite stays green
- [ ] `.github/workflows/arch-check.yml` parses cleanly and runs successfully against a test branch
- [ ] PR with a synthetic import cycle fails CI (dogfooded once)
- [ ] `CLAUDE.md` explains how to change the CI trigger scope
- [ ] `codegraph/docs/arch-policies.md` documents all 3 built-in policies with false-positive notes
