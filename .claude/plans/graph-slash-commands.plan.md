# Plan: Graph-Powered Slash Commands

## Summary

Ship 5 read-only slash commands (`/blast-radius`, `/dead-code`, `/who-owns`, `/trace-endpoint`, `/arch-check`) that wrap canonical Cypher patterns over the existing codegraph Neo4j graph. Each is a single markdown file in `.claude/commands/` that mirrors the frontmatter + narrative structure of the existing `/graph` and `/graph-refresh` commands. Zero new code — pure curation of query patterns we've already proven out during this session's audit. The result: daily dev tasks (rename blast radius, dead-code sweeps, ownership lookup, endpoint traces, architecture conformance) become one-shot commands instead of hand-written Cypher.

## User Story

As a developer (or Claude Code agent) using codegraph to navigate the repo,
I want pre-packaged slash commands for the most common graph questions,
so that "who depends on X?", "what's dead?", "who owns this?", "what does this endpoint touch?", and "does the architecture still hold?" each take one command instead of hand-written Cypher.

## Metadata

| Field | Value |
|-------|-------|
| Type | NEW_CAPABILITY (tooling layer over existing graph) |
| Complexity | LOW |
| Systems Affected | `.claude/commands/` only; optional update to `CLAUDE.md` |
| New Dependencies | None — everything runs through the existing `codegraph query` CLI |
| Generalized | Specific: each command targets a distinct question. A generalized `/graph-recipe <name>` would hurt discoverability — slash-command autocomplete is the point. |
| Depends On | `glimmering-painting-yao.md` — already merged; added Python `CALLS` edges and function `DECORATED_BY` wiring that `/dead-code` and `/blast-radius` rely on |

---

## Reuse Inventory

| What Exists | File:Line | How This Plan Reuses It |
|-------------|-----------|--------------------------|
| `codegraph query --json "<cypher>"` CLI | `codegraph/codegraph/cli.py` (the `query` Typer subcommand) | All 5 commands shell out to this binary verbatim |
| `/graph` slash command template | `.claude/commands/graph.md` | Mirror frontmatter + `$ARGUMENTS` injection pattern |
| `/graph-refresh` slash command template | `.claude/commands/graph-refresh.md` | Mirror "## Why / ## What this does / ## After running" narrative layout |
| Graph schema (nodes + edges) | `codegraph/codegraph/schema.py:13-293` | Every query references existing labels/edges — no new node or edge types needed |
| `:Function -[:DECORATED_BY]-> :Decorator` routing | `codegraph/codegraph/loader.py` (post-B1 fix) | `/dead-code` uses `DECORATED_BY` to distinguish Typer/MCP entry-point functions from true orphans |
| `:Method -[:CALLS]-> :Method` edges for Python | `codegraph/codegraph/py_parser.py` (post-B2 fix) | `/blast-radius` and `/trace-endpoint` traverse CALLS |
| Ownership edges (`LAST_MODIFIED_BY`, `CONTRIBUTED_BY`, `OWNED_BY`) | `codegraph/codegraph/loader.py:792-815` | `/who-owns` joins them |
| `:Endpoint` + `:Method -[:HANDLES]-> :Endpoint` | `codegraph/codegraph/parser.py` (TS only, Stage 1) + loader | `/trace-endpoint` starts traversal from `:Endpoint` nodes |

**Assessment: 100% wiring — no new infrastructure.** Pure query curation + markdown authorship.

---

## Patterns to Follow

### Slash-command frontmatter + argument injection
```markdown
# SOURCE: .claude/commands/graph.md:1-3, 82-86
---
allowed-tools: Bash(codegraph query:*), Bash(codegraph/.venv/bin/codegraph query:*), Bash(.venv/bin/codegraph query:*)
description: <one-line capability>
---
...
codegraph/.venv/bin/codegraph query --json "$ARGUMENTS"
```
`$ARGUMENTS` captures everything after the slash-command name. For commands that take a symbol name (e.g. `/blast-radius IgnoreFilter`), interpolate into the Cypher literal before shelling out.

### Narrative structure (mirror `/graph-refresh`)
```markdown
# SOURCE: .claude/commands/graph-refresh.md:6-26
## Why
<when to use + motivation>

## What this does
<the bash invocation in a fenced block>

## After running
<how to interpret / follow-up queries>
```

### Blast-radius pattern (multi-edge union)
```cypher
# SOURCE: .claude/commands/graph.md:33-36 (IMPORTS_SYMBOL pattern)
MATCH (caller:File)-[r:IMPORTS_SYMBOL]->(source:File)
WHERE r.symbol = 'IgnoreFilter'
RETURN caller.path, source.path
```
Generalized for `/blast-radius`: union with CALLS (method→method), EXTENDS (class heritage), INJECTS (DI), RENDERS (React).

### Dead-code pattern (negated-relationship filter)
```cypher
# The pattern: "no incoming CALLS, no incoming RENDERS, not a framework entry point"
MATCH (f:Function)
WHERE NOT EXISTS { ()-[:CALLS]->(f) }
  AND NOT EXISTS { ()-[:RENDERS]->(f) }
  AND NOT EXISTS { (f)-[:DECORATED_BY]->(:Decorator) }
  AND f.file STARTS WITH $path_prefix
RETURN f.name, f.file
```
Critical: without the `DECORATED_BY` exclusion, every `@mcp.tool()` and `@app.command()` shows up as a false positive.

### Ownership join pattern
```cypher
# SOURCE: codegraph/codegraph/loader.py:790-815 (LAST_MODIFIED_BY / CONTRIBUTED_BY / OWNED_BY writes)
MATCH (f:File {path: 'codegraph/codegraph/loader.py'})
OPTIONAL MATCH (f)-[lm:LAST_MODIFIED_BY]->(last:Author)
OPTIONAL MATCH (f)-[c:CONTRIBUTED_BY]->(co:Author)
OPTIONAL MATCH (f)-[:OWNED_BY]->(t:Team)
RETURN last.name, last.email,
       collect(DISTINCT {author: co.name, commits: c.commits}) AS contributors,
       collect(DISTINCT t.name) AS teams
```

### Endpoint-to-entity trace
```cypher
MATCH (e:Endpoint) WHERE e.path CONTAINS '/users'
MATCH (m:Method)-[:HANDLES]->(e)
OPTIONAL MATCH path = (m)-[:CALLS*1..4]->(target:Method)
OPTIONAL MATCH (enclosing:Class)-[:HAS_METHOD]->(target)
RETURN e.method, e.path, m.name AS handler,
       collect(DISTINCT enclosing.name) AS classes_touched
```

### Architecture conformance (cycle detection)
```cypher
# File-import cycles
MATCH path = (a:File)-[:IMPORTS*2..6]->(a)
RETURN [n IN nodes(path) | n.path] AS cycle
LIMIT 20
```

---

## Files to Change

| File | Action | Purpose |
|------|--------|---------|
| `.claude/commands/blast-radius.md` | CREATE | Rename/delete impact check |
| `.claude/commands/dead-code.md` | CREATE | Orphan functions/classes/atoms sweep |
| `.claude/commands/who-owns.md` | CREATE | File ownership + contributor report |
| `.claude/commands/trace-endpoint.md` | CREATE | Endpoint → handler → data-reach trace |
| `.claude/commands/arch-check.md` | CREATE | Built-in conformance policies (cycles, layering) |
| `CLAUDE.md` | UPDATE | Add a "Daily power-tool commands" bullet list in the "Using the graph during development" section, pointing at the 5 new commands with one-line use cases |

**No code changes.** No tests in the traditional sense — dogfooding validation (Task V1) replaces pytest.

---

## Tasks

Execute in order. Each task produces a single file and is independently verifiable by running the command against the live graph.

### Task 1: Create `/blast-radius`

- **File**: `.claude/commands/blast-radius.md`
- **Action**: CREATE
- **Why not UPDATE?**: New slash command; `/graph` is the generic escape hatch, this one is purpose-built for the specific blast-radius pattern.
- **Read first**: `.claude/commands/graph.md:1-88` — the canonical template, and a working blast-radius example at lines 22-36 to mirror.
- **Implement**:
  - Frontmatter: `allowed-tools: Bash(codegraph query:*), Bash(codegraph/.venv/bin/codegraph query:*), Bash(.venv/bin/codegraph query:*)` + `description: "Show everything that depends on a symbol (class/function/method) — run before renaming, deleting, or moving."`
  - `## Usage`: `/blast-radius <SymbolName>` — one positional arg, interpreted as a class OR function OR method name (no disambiguation needed; the union query handles all three).
  - `## What this does`: ship a single Cypher UNION that returns `{edge_kind, caller_file, caller_symbol, callee_file, callee_symbol}` rows for:
    1. `IMPORTS_SYMBOL` where `r.symbol = $ARGUMENTS` (file-level import sites)
    2. `CALLS` where the callee `:Method.name = $ARGUMENTS` (typed call sites)
    3. `EXTENDS` where parent `:Class.name = $ARGUMENTS` (subclasses that'd break)
    4. `INJECTS` where target `:Class.name = $ARGUMENTS` (NestJS DI consumers)
    5. `RENDERS` where target `:Function.name = $ARGUMENTS` (React parent components)
  - Wrap in a single query with `UNION ALL` and `LIMIT 200` by default — note in the narrative how to raise the limit.
  - `## Running it`: the `codegraph query --json "..."` invocation with `$ARGUMENTS` interpolated into each `WHERE` clause.
  - `## Caveats`: name collisions (two classes with the same name in different packages both show up — that's a feature, not a bug); IMPORT edges from aliased imports may miss if the graph doesn't track re-exports transitively.
- **Mirror**: `.claude/commands/graph-refresh.md:1-37` (narrative shape) + `.claude/commands/graph.md:33-36` (IMPORTS_SYMBOL query shape).
- **Validate**: `codegraph/.venv/bin/codegraph query "<interpolated cypher with symbol='IgnoreFilter'>"` — must return non-empty rows (we know `IgnoreFilter` has real callers).

### Task 2: Create `/dead-code`

- **File**: `.claude/commands/dead-code.md`
- **Action**: CREATE
- **Why not UPDATE?**: New command; no existing dead-code tooling.
- **Read first**: `codegraph/codegraph/schema.py:233-293` (the full edge-kind list — the query negates all incoming relationship types); `codegraph/codegraph/loader.py:642-660` (the post-B1 Function `DECORATED_BY` MERGE, which this query depends on).
- **Implement**:
  - Frontmatter same as Task 1, `description: "List orphan functions, classes, atoms, and endpoints with no inbound references — after excluding framework entry points."`
  - `## Usage`: `/dead-code [path_prefix]` — optional scope arg; default to `codegraph/codegraph/` if blank.
  - `## What this does`: ship four separate Cypher queries, unioned in the output with a `kind` discriminator column:
    1. **Orphan functions**: `:Function` with no incoming CALLS, no RENDERS, AND no outgoing DECORATED_BY to any `:Decorator` (this excludes `@mcp.tool()`, `@app.command()`, `@pytest.fixture`, etc.)
    2. **Orphan classes**: `:Class` with no incoming EXTENDS, INJECTS, RESOLVES, or IMPORTS_SYMBOL
    3. **Orphan atoms**: `:Atom` with no incoming READS_ATOM or WRITES_ATOM
    4. **Orphan endpoints**: `:Endpoint` with no incoming HANDLES
  - Each row: `{kind, name, file}`, filtered by the optional path prefix.
  - `## Running it`: bash block with `$ARGUMENTS` defaulting to `codegraph/codegraph/` via shell `${ARGUMENTS:-codegraph/codegraph/}`.
  - `## Caveats`: (a) module-level Typer commands in Python are caught by the decorator filter, but a decorated function whose decorator isn't parsed (edge case) will still appear; (b) Python lacks CALLS edges from module-level functions to methods (resolver limitation) — so some "orphan functions" may actually be called from module-level code. Document the limitation.
- **Mirror**: `.claude/commands/graph.md:55-78` (the decorator + file-scoped query style).
- **Validate**: Run against `codegraph/codegraph/` — should return a short, plausibly-empty list (we expect near-zero orphans in a healthy codebase). If it returns e.g. `_unused_helper` or similar, confirm by grep that nothing references it.

### Task 3: Create `/who-owns`

- **File**: `.claude/commands/who-owns.md`
- **Action**: CREATE
- **Why not UPDATE?**: New command; ownership data is on every `:File` node but has no ergonomic query wrapper.
- **Read first**: `codegraph/codegraph/loader.py:774-815` (ownership write paths — confirms the edge shapes, edge properties like `at` and `commits`).
- **Implement**:
  - Frontmatter: `description: "Show the latest author, top contributors, and CODEOWNERS team for a file."`
  - `## Usage`: `/who-owns <path>` — path relative to repo root, exactly as stored in `:File {path}`.
  - `## What this does`: single query joining:
    - `(f:File {path: $ARGUMENTS})-[lm:LAST_MODIFIED_BY]->(last:Author)`
    - `(f)-[c:CONTRIBUTED_BY]->(co:Author)` — collect, sort by `c.commits` desc, top 5
    - `(f)-[:OWNED_BY]->(t:Team)` — collect team names
  - Return: last-modifier name+email+date, contributor roll-up, team list.
  - `## Caveats`: if the graph was indexed with `--skip-ownership` (as `/graph-refresh` does by default), all three optional matches will return nulls. Document: "if empty, re-index without `--skip-ownership`."
- **Mirror**: `.claude/commands/graph.md:46-53` (class→methods join idiom).
- **Validate**: `codegraph query` against `codegraph/codegraph/loader.py` — should return non-null author info iff the graph has ownership edges. (Earlier graph snapshot had 13,460 LAST_MODIFIED_BY edges, so this is exercise-able once ownership is re-populated.)

### Task 4: Create `/trace-endpoint`

- **File**: `.claude/commands/trace-endpoint.md`
- **Action**: CREATE
- **Why not UPDATE?**: New command; most useful for Twenty (TS) today, ready for future Python framework support.
- **Read first**: `codegraph/codegraph/schema.py:133-143` (EndpointNode shape — id includes `method:path@file#handler`); `codegraph/codegraph/resolver.py:462-476` (how CALLS_ENDPOINT gets resolved from URL patterns).
- **Implement**:
  - Frontmatter: `description: "From a URL substring, show the matched endpoint(s), handler methods, and everything they transitively call."`
  - `## Usage`: `/trace-endpoint <url_substring>` — matches against `:Endpoint.path CONTAINS $ARGUMENTS`.
  - `## What this does`: two queries:
    1. **The surface**: endpoint row — method, path, controller class, handler method.
    2. **The reach**: `MATCH (m:Method)-[:HANDLES]->(e:Endpoint) WHERE e.path CONTAINS $ARGUMENTS MATCH path = (m)-[:CALLS*1..4]->(target:Method) MATCH (cls:Class)-[:HAS_METHOD]->(target) RETURN DISTINCT cls.name AS class, target.name AS method, target.file AS file ORDER BY class, method` — shows every method reachable within 4 hops.
  - `## Caveats`: CALLS edges only exist for typed method-to-method calls; polymorphic / runtime-dispatch paths aren't traced. Raise the `*1..4` bound for longer traces at your own performance risk.
- **Mirror**: existing Twenty endpoint queries (document in the command's "## Related" section).
- **Validate**: against Twenty, `/trace-endpoint /users` should return rows (the users controller). Against codegraph's own package, 0 rows (no Python endpoint detection in Stage 1) — document this as expected and note that FastAPI/Flask detection is Stage 2.

### Task 5: Create `/arch-check`

- **File**: `.claude/commands/arch-check.md`
- **Action**: CREATE
- **Why not UPDATE?**: New command; designed as a ship-with-defaults policy runner with a documented extension pattern.
- **Read first**: `codegraph/codegraph/schema.py:237-267` (edge kinds the policies rely on: IMPORTS, INJECTS, DECLARES_CONTROLLER, etc.).
- **Implement**:
  - Frontmatter: `description: "Run built-in architecture-conformance policies against the graph — cycles, cross-package imports, layer violations."`
  - `## Usage`: `/arch-check` (no args).
  - `## What this does`: run three policies, return a count + up to 10 violation rows per policy:
    1. **Import cycles**: `MATCH path = (a:File)-[:IMPORTS*2..6]->(a) RETURN [n IN nodes(path) | n.path] AS cycle LIMIT 10`
    2. **Cross-package violation** (placeholder example — document how to add more): `MATCH (a:File)-[:IMPORTS]->(b:File) WHERE a.package = 'twenty-front' AND b.package = 'twenty-server' RETURN a.path, b.path LIMIT 10`
    3. **Controller → Repository bypass** (skips Service layer): `MATCH (ctrl:Controller)-[:HAS_METHOD]->(m:Method)-[:CALLS*1..3]->(repo:Class) WHERE repo.name ENDS WITH 'Repository' AND NOT EXISTS { (m)-[:CALLS*]->(:Class) WHERE <intermediate is named *Service> } RETURN ctrl.name, repo.name LIMIT 10`
  - `## Extending it`: document the pattern — users can fork the file and add policy blocks; each policy is a labeled Cypher block with a title.
  - `## Caveats`: policies are project-specific. The default set is tuned for Twenty + codegraph; a fresh repo will see noise. Document explicitly.
- **Mirror**: `.claude/commands/graph-refresh.md` narrative style (Why / What this does / After running).
- **Validate**: run against the current graph; the cycle query should return zero rows (we'd want to know if it doesn't — that's a finding).

### Task 6: Update `CLAUDE.md`

- **File**: `CLAUDE.md`
- **Action**: UPDATE
- **Why not CREATE?**: `CLAUDE.md` already documents `/graph` and `/graph-refresh`; new commands belong in the same section.
- **Read first**: `CLAUDE.md:11-49` (the "Using the graph during development" section).
- **Implement**: after the `/graph-refresh` subsection, add a new subsection `### Daily power-tool commands` listing each of the 5 new commands with a one-line description and example invocation. Preserve the existing `/graph` and `/graph-refresh` docs.
- **Mirror**: the tone + depth of the existing `/graph` + `/graph-refresh` sections.
- **Validate**: `grep -E '^/(blast-radius|dead-code|who-owns|trace-endpoint|arch-check)' CLAUDE.md` returns 5 matches.

### Task V1: Dogfood validation

- **File**: none (runtime check only)
- **Action**: VERIFY
- **Implement**: after tasks 1-6 are done, run each command against the live graph with a representative argument and confirm expected behavior:
  - `/blast-radius IgnoreFilter` → non-empty, at least 1 caller
  - `/dead-code codegraph/codegraph/` → expected-small-list (0-3 genuine orphans max)
  - `/who-owns codegraph/codegraph/loader.py` → either full ownership report or a documented "re-index without --skip-ownership" notice
  - `/trace-endpoint /users` → non-empty for Twenty (if indexed), empty+documented for codegraph
  - `/arch-check` → runs clean (zero cycles), prints the three policies with their counts
- **Validate**: user reviews each command's output side-by-side with the equivalent hand-written query from this session's audit.

---

## Validation

```bash
# File existence
ls .claude/commands/{blast-radius,dead-code,who-owns,trace-endpoint,arch-check}.md

# Frontmatter well-formed (all five should have exactly two `---` lines)
grep -c '^---$' .claude/commands/blast-radius.md  # → 2
# (repeat for the others)

# Live dogfood — each should run without Cypher parse errors
codegraph/.venv/bin/codegraph query "$(<each command's hero query>)"

# CLAUDE.md update landed
grep -c '^### Daily power-tool commands' CLAUDE.md  # → 1
```

No pytest — this layer is configuration, not code. The `V1` task is the acceptance gate.

---

## Agent Team

1 feature agent + 2 standing agents (tester-e2e skipped — this is a CLI/config repo with no web UI).

### Feature Agents

#### Agent: commands

- **Owns**: `.claude/commands/blast-radius.md`, `.claude/commands/dead-code.md`, `.claude/commands/who-owns.md`, `.claude/commands/trace-endpoint.md`, `.claude/commands/arch-check.md`, `CLAUDE.md`
- **Does NOT touch**: anything under `codegraph/codegraph/` (no code changes); `.claude/commands/graph.md` and `.claude/commands/graph-refresh.md` (reference-only, do not modify)
- **Responsibilities**:
  - Author all 5 command markdown files, consistent frontmatter + narrative structure
  - Ensure every query is copy-pasteable into `codegraph query` without modification
  - Document caveats honestly (especially the `--skip-ownership` case and Stage-2 Python-framework gaps)
  - Update `CLAUDE.md` to list the new commands
- **Publishes contract**: a consistent slash-command shape: 2-line YAML frontmatter + `## Usage` + `## What this does` + `## Caveats` sections. Any future `/graph-*` command should mirror this.
- **Consumes contract from**: none (builds on the completed glimmering-painting-yao.md plan's outputs, specifically Python CALLS edges and Function DECORATED_BY persistence)
- **Validation**: all 5 command files exist; each hero query runs without Cypher parse errors against the live graph

### Standing Agents

#### Agent: general

- **Owns**: any cross-cutting doc updates beyond `CLAUDE.md` (e.g., if a README reference needs updating)
- **Does NOT touch**: the 5 command files (owned by the commands agent)
- **Responsibilities**: glue work if the commands agent surfaces a doc-level inconsistency elsewhere in the repo (e.g., stale `.agents/` vs `.claude/plans/` note)
- **Validation**: `git diff CLAUDE.md` shows only intended additions

#### Agent: tester-unit

- **Owns**: `codegraph/tests/` — may add a lightweight test (e.g., `test_commands_valid.py`) that parses each command's frontmatter and sanity-checks the embedded Cypher with `EXPLAIN` to catch syntax errors at CI time
- **Does NOT touch**: the command markdown files themselves
- **Responsibilities**:
  - Optional: add a `test_commands_valid.py` that globs `.claude/commands/*.md`, extracts the Cypher blocks, runs `EXPLAIN <cypher>` against the Neo4j test container, asserts no parse errors
  - Run `pytest tests/ -q` and confirm zero regressions in the 166-test baseline
- **Runs after**: the commands agent completes all 5 files
- **Validation**: `codegraph/.venv/bin/python -m pytest tests/ -q` — pass, zero warnings

#### Agent: tester-e2e

- **N/A for this plan.** No web UI, no user flow to exercise in a browser. Skip at spawn time.

### Spawn Order

1. **commands agent** — runs all 6 implementation tasks (5 command files + CLAUDE.md), then hands off
2. **general agent** — idle unless the commands agent surfaces cross-cutting doc work
3. **tester-unit** — runs after commands agent, executes dogfood validation (Task V1), optionally adds the command-validity test

### Cross-Cutting Concerns

| Concern | Owner | Detail |
|---------|-------|--------|
| Consistent frontmatter shape | commands | All 5 files MUST have identical `allowed-tools:` lines for predictable permission-prompt UX |
| `$ARGUMENTS` vs positional parsing | commands | Claude Code injects the full argument string into `$ARGUMENTS`; commands must handle empty input gracefully (print usage + exit 0) |
| LIMIT defaults | commands | Every query carries `LIMIT 200` (or smaller) by default; document the pattern for raising it |
| Python vs TS asymmetry | commands | `/trace-endpoint` and parts of `/blast-radius` rely on edges Python doesn't emit yet (Endpoint, CALLS_ENDPOINT). Document "Python coverage: Stage 1" in each affected command's caveats |

---

## Acceptance Criteria

- [ ] 5 command files exist in `.claude/commands/`, each with valid YAML frontmatter
- [ ] Each command's hero Cypher parses without error via `codegraph query`
- [ ] `/blast-radius IgnoreFilter` returns ≥1 row against the live graph
- [ ] `/dead-code codegraph/codegraph/` returns ≤5 rows (healthy repo baseline)
- [ ] `/who-owns <path>` returns correctly-shaped output or the documented "re-index without `--skip-ownership`" notice
- [ ] `/trace-endpoint /users` behaves as documented (non-empty if Twenty is indexed; empty + explanatory output otherwise)
- [ ] `/arch-check` runs all three policies; each reports pass or reports violations with file paths
- [ ] `CLAUDE.md` lists all 5 new commands in the "Using the graph during development" section
- [ ] Existing 166-test pytest suite stays green, zero warnings
- [ ] No changes under `codegraph/codegraph/` (this plan is pure config)
