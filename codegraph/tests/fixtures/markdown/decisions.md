# ADR-001: Use Neo4j as the graph backend

## Status

Accepted

## Context

We need a property-graph database that supports Cypher queries,
transactional writes, and can run locally via Docker with zero
cloud dependencies. The main alternatives considered were:

- Neo4j Community Edition (free, mature, Cypher)
- Amazon Neptune (managed, Gremlin-based, cloud-only)
- ArangoDB (multi-model, AQL, self-hosted)

## Decision

Use Neo4j Community Edition 5.x running in a Docker container
exposed on port 7688 (Bolt) and 7475 (HTTP).

## Rationale

Neo4j is the most mature property graph with the richest Cypher
ecosystem. Running locally avoids cloud lock-in and keeps the
tool usable offline. The Community Edition license (GPLv3) is
acceptable for a developer tool.

# ADR-002: Two concrete parsers, not one generic

## Status

Proposed

## Context

We support both TypeScript and Python. A single generic parser
would require abstracting over fundamentally different AST shapes.

## Decision

Maintain two concrete parsers (TsParser, PyParser) that both emit
the same ParseResult dataclass.

## Rationale

Depth over abstraction: each parser can exploit language-specific
tree-sitter queries without compromise. The shared ParseResult
interface keeps the loader and resolver language-agnostic.
