# Graph Indexing

Graph indexing is the process of walking a codebase with tree-sitter,
extracting typed nodes (files, classes, functions, methods) and edges
(imports, calls, extends), and loading them into a Neo4j property graph.
The resulting graph enables structural queries that are impossible with
text search alone.

## Incremental Update

Incremental update uses SHA256 content hashing to skip unchanged files
during re-indexing. When a file's hash matches the cached value, its
parse result is loaded from the on-disk cache instead of re-parsing.
This reduces re-index time from minutes to seconds for large codebases.

## Schema Migration

Schema migration ensures that Neo4j constraints and indexes stay in sync
with the evolving node and edge types. Migrations run at the start of
every `codegraph index` invocation, dropping obsolete constraints and
creating new ones idempotently via `IF NOT EXISTS` / `IF EXISTS` guards.
