# Architecture-Conformance Policies

Reference for the three built-in policies run by `codegraph arch-check` and the `/arch-check` slash command. Each section covers: what the policy detects, why the invariant matters, the exact Cypher, how to interpret a violation, and common false positives.

If a policy fires on your PR and you believe it shouldn't, read the "False positives" subsection first — most violations are either load-bearing bugs or one of the known noise patterns, and the doc flags which is which.

---

## 1. `import_cycles`

### What it detects

File-level import cycles of length 2-6. A cycle is a sequence of `:File` nodes connected by `:IMPORTS` edges that returns to its starting point:

```cypher
MATCH path = (a:File)-[:IMPORTS*2..6]->(a)
RETURN [n IN nodes(path) | n.path] AS cycle, length(path) AS hops
```

### Why it matters

Import cycles make modules untestable in isolation, break tree-shaking, and pin code into tight coupling that's expensive to split later. In Python they cause `ImportError` when the cycle is forced into different import orders; in TypeScript they silently turn exports into `undefined` at load time. Either way: they're almost never intentional.

### Interpreting a violation

Each row in the sample lists one cycle as a path: `["a.py", "b.py", "a.py"]` means `a.py` imports `b.py`, which imports `a.py`. The `hops` column is the cycle length — short cycles (2 hops, direct A↔B) are usually easier to break than long ones (3+ hops, fan through multiple modules).

**Typical resolutions**:
- Extract the shared types / constants into a new leaf module both can import
- Invert the dependency direction (dependency injection, event emitter, callback)
- Merge the two modules if they're genuinely that coupled

### False positives

- **Re-export barrels**: `__init__.py` / `index.ts` files that re-export symbols from the module directory can form cycles with their submodules. Indexer treats them as first-class files; the cycle is real but mostly harmless. Consider refactoring to a dedicated barrel (`_exports.py`) that doesn't import from the submodules.
- **Conditional imports under `if TYPE_CHECKING:`**: the parser records these as regular imports. If the cycle only exists at type-check time (never at runtime), it's less urgent — but still worth refactoring since the types are coupled.

---

## 2. `cross_package`

### What it detects

Imports that cross a forbidden package boundary. The default set has one pair:

```python
CROSS_PACKAGE_PAIRS = [
    ("twenty-front", "twenty-server"),
]
```

Any `:File` in `twenty-front` that imports from `:File` in `twenty-server` is a violation:

```cypher
MATCH (a:File)-[:IMPORTS]->(b:File)
WHERE a.package = 'twenty-front' AND b.package = 'twenty-server'
RETURN a.path AS importer, b.path AS importee
```

### Why it matters

Frontend and backend have different runtimes, different bundlers, different security models. Importing backend code into the frontend either breaks the build or (worse) leaks server-only secrets into the client bundle. The rule is a hard boundary — if you need to share something, extract it to a shared package (`twenty-types`, `twenty-shared`).

### Extending the rule set

Edit `codegraph/codegraph/arch_check.py` and append tuples to `CROSS_PACKAGE_PAIRS`:

```python
CROSS_PACKAGE_PAIRS = [
    ("twenty-front", "twenty-server"),
    ("packages/docs", "packages/twenty-server"),   # docs must not import from app
    ("packages/twenty-types", "packages/twenty-server"),  # shared types must be leaf
]
```

Every pair becomes an additional violation bucket. The report aggregates them so CI output stays compact.

### Interpreting a violation

`importer` is the file that broke the rule; `importee` is the file it shouldn't have reached. `importer_package` / `importee_package` tell you which boundary was crossed (useful once you have multiple rules).

**Typical resolutions**:
- Move the needed symbol into a shared leaf package both sides can import from
- Duplicate the constant or type (if it's small and stable — accept the minor drift cost)
- Replace the direct import with an HTTP call if the boundary is actually runtime, not build-time

### False positives

- **Auto-generated files**: a codegen step that writes generated types into the wrong package directory can trip this. Either fix the codegen target dir, or exclude generated files by `path` pattern before indexing.
- **Monorepo tooling**: if Turborepo / Nx / Lerna creates symlinks between packages, the indexer may follow them. Check that your `.codegraphignore` excludes `node_modules` and symlink-dense directories.

---

## 3. `layer_bypass`

### What it detects

Controllers that reach a `*Repository` method within 3 hops of `CALLS` without traversing a `*Service`:

```cypher
MATCH (ctrl:Controller)-[:HAS_METHOD]->(m:Method)-[:CALLS*1..3]->(target:Method)
MATCH (repo:Class)-[:HAS_METHOD]->(target)
WHERE repo.name ENDS WITH 'Repository'
  AND NOT EXISTS {
    MATCH (ctrl)-[:HAS_METHOD]->(:Method)-[:CALLS*1..3]->(:Method)<-[:HAS_METHOD]-(svc:Class)
    WHERE svc.name ENDS WITH 'Service'
  }
RETURN ctrl.name AS controller, repo.name AS repository, target.name AS method
```

### Why it matters

The Controller → Service → Repository layering is the default pattern in NestJS and most backend frameworks for a reason. Services are where business rules, validation, transaction boundaries, and cross-cutting concerns (auditing, caching, authorization) live. When a Controller talks to a Repository directly, all of that is bypassed — and the bypass usually stays hidden until someone needs to add one of those concerns and has to audit every call site.

### Interpreting a violation

`controller` is the class with a HAS_METHOD → CALLS chain into a Repository. `repository` is which Repository it reached. `method` is the specific repo method called. The violation is "controller X short-circuits directly to repo Y to call Z".

**Typical resolutions**:
- Introduce a `*Service` between the Controller and the Repository if one doesn't exist
- Move the call site from the Controller into an existing Service
- If the Controller really does need direct Repository access (e.g. a health-check endpoint reading `_metadata`), document it explicitly and either suppress by renaming or refine the policy

### False positives

- **Naming-convention-only detection**: this policy is a name-based heuristic. A project using `*Manager`, `*Handler`, or `*Facade` as its service layer won't match the `ENDS WITH 'Service'` check. Fork the module and rename to match.
- **Health checks / liveness probes**: a Controller calling `repository.countAll()` to prove the DB is reachable is a legitimate bypass. Consider renaming the repository method (`__healthcheck_countAll`), moving the check into a dedicated `HealthService`, or excluding that Controller by name.
- **Pure read endpoints with no business rules**: if an endpoint genuinely does nothing but read and return, inserting a pass-through Service adds ceremony without value. Accept the one-off violation or relax the policy to only fire on write operations.

---

## Exit codes

`codegraph arch-check` returns:
- **0** — every policy passed.
- **1** — one or more policies reported violations.

CI job fails on non-zero. Report artifact (`arch-report.json`) is always uploaded, even on failure, so you can inspect the samples without re-running.

## Adding a new policy

For v1, extending means forking `codegraph/codegraph/arch_check.py` and adding a new `_check_<policy_name>` function. It should:
1. Take a `neo4j.Driver` argument.
2. Run a count query and a sample query (limit via `SAMPLE_LIMIT`).
3. Return a `PolicyResult`.
4. Be appended to the list in `run_arch_check`.

Stage 2 will add a `.arch-policies.toml` loader so per-repo extensions don't require a fork. Until then, fork-and-maintain is the expected path.
