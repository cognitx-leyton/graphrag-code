# codegraph

Map a TypeScript codebase into Neo4j with NestJS + React awareness, then query it.

## Quick start

```bash
cd codegraph

# 1. Python env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Neo4j
docker compose up -d
# Browser UI:  http://localhost:7475   (neo4j / codegraph123)
# Bolt:        bolt://localhost:7688

# 3. Index easy-builder
.venv/bin/python -m codegraph.cli index /home/edouard-gouilliard/easy-builder

# 4. Validate
.venv/bin/python -m codegraph.cli validate /home/edouard-gouilliard/easy-builder

# 5. Ad-hoc query
.venv/bin/python -m codegraph.cli query "MATCH (e:Endpoint) RETURN e.method, e.path LIMIT 10"
```

See `queries.md` for example queries.

## Schema

Nodes: `File`, `Class`, `Function`, `Interface`, `Endpoint`, `Hook`, `Decorator`, `External`
Edges: `IMPORTS`, `IMPORTS_EXTERNAL`, `DEFINES_CLASS`, `DEFINES_FUNC`, `DEFINES_IFACE`, `EXPOSES`, `INJECTS`, `EXTENDS`, `IMPLEMENTS`, `RENDERS`, `USES_HOOK`, `DECORATED_BY`
