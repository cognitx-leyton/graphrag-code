"""Neo4j writer: constraints, batched UNWIND-MERGE, idempotent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from neo4j import Driver, GraphDatabase

from .resolver import Index
from .schema import (
    DECORATED_BY,
    DEFINES_CLASS,
    DEFINES_FUNC,
    DEFINES_IFACE,
    Edge,
    EXPOSES,
    EXTENDS,
    IMPLEMENTS,
    IMPORTS,
    INJECTS,
    RENDERS,
    USES_HOOK,
)


BATCH = 1000


_CONSTRAINTS = [
    "CREATE CONSTRAINT file_path IF NOT EXISTS FOR (n:File) REQUIRE n.path IS UNIQUE",
    "CREATE CONSTRAINT class_id IF NOT EXISTS FOR (n:Class) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT func_id IF NOT EXISTS FOR (n:Function) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT iface_id IF NOT EXISTS FOR (n:Interface) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT endpoint_id IF NOT EXISTS FOR (n:Endpoint) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT external_spec IF NOT EXISTS FOR (n:External) REQUIRE n.specifier IS UNIQUE",
    "CREATE CONSTRAINT hook_name IF NOT EXISTS FOR (n:Hook) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT decorator_name IF NOT EXISTS FOR (n:Decorator) REQUIRE n.name IS UNIQUE",
]

_INDEXES = [
    "CREATE INDEX class_name IF NOT EXISTS FOR (n:Class) ON (n.name)",
    "CREATE INDEX func_name IF NOT EXISTS FOR (n:Function) ON (n.name)",
    "CREATE INDEX file_package IF NOT EXISTS FOR (n:File) ON (n.package)",
    "CREATE INDEX endpoint_path IF NOT EXISTS FOR (n:Endpoint) ON (n.path)",
    "CREATE INDEX class_file IF NOT EXISTS FOR (n:Class) ON (n.file)",
]


@dataclass
class LoadStats:
    files: int = 0
    classes: int = 0
    functions: int = 0
    interfaces: int = 0
    endpoints: int = 0
    externals: int = 0
    edges: dict[str, int] = None

    def __post_init__(self) -> None:
        if self.edges is None:
            self.edges = {}


class Neo4jLoader:
    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j") -> None:
        self.driver: Driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database

    def close(self) -> None:
        self.driver.close()

    # -- schema --------------------------------------------------------

    def init_schema(self) -> None:
        with self.driver.session(database=self.database) as s:
            for stmt in _CONSTRAINTS + _INDEXES:
                s.run(stmt)

    def wipe(self) -> None:
        """Drop everything in the target database."""
        with self.driver.session(database=self.database) as s:
            s.run("MATCH (n) DETACH DELETE n")

    # -- load ---------------------------------------------------------

    def load(self, index: Index, edges: list[Edge]) -> LoadStats:
        stats = LoadStats()
        # Partition things
        files = [r.file for r in index.files_by_path.values()]
        classes = [c for r in index.files_by_path.values() for c in r.classes]
        funcs = [f for r in index.files_by_path.values() for f in r.functions]
        ifaces = [i for r in index.files_by_path.values() for i in r.interfaces]
        endpoints = [e for r in index.files_by_path.values() for e in r.endpoints]

        externals: set[str] = set()
        for e in edges:
            if e.kind == IMPORTS and e.dst_id.startswith("external:"):
                externals.add(e.dst_id[len("external:"):])

        hooks: set[str] = set()
        decorators: set[str] = set()
        for e in edges:
            if e.kind == USES_HOOK:
                hooks.add(e.props.get("hook", ""))
            elif e.kind == DECORATED_BY:
                decorators.add(e.dst_id[len("dec:"):])

        stats.files = len(files)
        stats.classes = len(classes)
        stats.functions = len(funcs)
        stats.interfaces = len(ifaces)
        stats.endpoints = len(endpoints)
        stats.externals = len(externals)

        with self.driver.session(database=self.database) as s:
            # Nodes
            _run_batched(s, """
                UNWIND $rows AS r
                MERGE (n:File {path: r.path})
                SET n.package = r.package,
                    n.language = r.language,
                    n.loc = r.loc,
                    n.is_controller = r.is_controller,
                    n.is_injectable = r.is_injectable,
                    n.is_module = r.is_module,
                    n.is_component = r.is_component
            """, [
                dict(path=f.path, package=f.package, language=f.language, loc=f.loc,
                     is_controller=f.is_controller, is_injectable=f.is_injectable,
                     is_module=f.is_module, is_component=f.is_component)
                for f in files
            ])

            _run_batched(s, """
                UNWIND $rows AS r
                MERGE (n:Class {id: r.id})
                SET n.name = r.name,
                    n.file = r.file,
                    n.is_controller = r.is_controller,
                    n.is_injectable = r.is_injectable,
                    n.is_module = r.is_module,
                    n.is_entity = r.is_entity,
                    n.is_abstract = r.is_abstract,
                    n.base_path = r.base_path
                WITH n, r
                MATCH (f:File {path: r.file})
                MERGE (f)-[:DEFINES_CLASS]->(n)
            """, [
                dict(id=c.id, name=c.name, file=c.file,
                     is_controller=c.is_controller, is_injectable=c.is_injectable,
                     is_module=c.is_module, is_entity=c.is_entity,
                     is_abstract=c.is_abstract, base_path=c.base_path)
                for c in classes
            ])

            _run_batched(s, """
                UNWIND $rows AS r
                MERGE (n:Function {id: r.id})
                SET n.name = r.name,
                    n.file = r.file,
                    n.is_component = r.is_component,
                    n.exported = r.exported
                WITH n, r
                MATCH (f:File {path: r.file})
                MERGE (f)-[:DEFINES_FUNC]->(n)
            """, [
                dict(id=f.id, name=f.name, file=f.file,
                     is_component=f.is_component, exported=f.exported)
                for f in funcs
            ])

            _run_batched(s, """
                UNWIND $rows AS r
                MERGE (n:Interface {id: r.id})
                SET n.name = r.name, n.file = r.file
                WITH n, r
                MATCH (f:File {path: r.file})
                MERGE (f)-[:DEFINES_IFACE]->(n)
            """, [
                dict(id=i.id, name=i.name, file=i.file)
                for i in ifaces
            ])

            _run_batched(s, """
                UNWIND $rows AS r
                MERGE (e:Endpoint {id: r.id})
                SET e.method = r.method,
                    e.path = r.path,
                    e.handler = r.handler,
                    e.file = r.file
                WITH e, r
                MATCH (c:Class {id: r.cls})
                MERGE (c)-[:EXPOSES]->(e)
            """, [
                dict(id=e.id, method=e.method, path=e.path, handler=e.handler,
                     file=e.file, cls=e.controller_class)
                for e in endpoints
            ])

            _run_batched(s, """
                UNWIND $rows AS r
                MERGE (:External {specifier: r.spec})
            """, [dict(spec=x) for x in externals])

            _run_batched(s, """
                UNWIND $rows AS r
                MERGE (:Hook {name: r.name})
            """, [dict(name=h) for h in hooks if h])

            _run_batched(s, """
                UNWIND $rows AS r
                MERGE (:Decorator {name: r.name})
            """, [dict(name=d) for d in decorators])

            # Edges
            imports_file = []
            imports_ext = []
            extends = []
            implements = []
            injects = []
            renders = []
            uses_hook = []
            decorated_class = []
            decorated_method = []
            for e in edges:
                if e.kind == DECORATED_BY:
                    dname = e.dst_id[len("dec:"):]
                    if e.src_id.startswith("class:"):
                        decorated_class.append(dict(src=e.src_id, name=dname))
                    elif e.src_id.startswith("method:"):
                        decorated_method.append(dict(src=e.src_id, name=dname))
                    continue
                if e.kind == IMPORTS:
                    if e.props.get("external"):
                        imports_ext.append(dict(
                            src=e.src_id[len("file:"):],
                            spec=e.dst_id[len("external:"):],
                            specifier=e.props.get("specifier", ""),
                            type_only=e.props.get("type_only", False),
                        ))
                    else:
                        imports_file.append(dict(
                            src=e.src_id[len("file:"):],
                            dst=e.dst_id[len("file:"):],
                            specifier=e.props.get("specifier", ""),
                            type_only=e.props.get("type_only", False),
                        ))
                elif e.kind == EXTENDS:
                    extends.append(dict(src=e.src_id, dst=e.dst_id))
                elif e.kind == IMPLEMENTS:
                    implements.append(dict(src=e.src_id, dst=e.dst_id))
                elif e.kind == INJECTS:
                    injects.append(dict(src=e.src_id, dst=e.dst_id))
                elif e.kind == RENDERS:
                    renders.append(dict(src=e.src_id, dst=e.dst_id))
                elif e.kind == USES_HOOK:
                    uses_hook.append(dict(src=e.src_id, hook=e.props.get("hook", "")))

            _run_batched(s, """
                UNWIND $rows AS r
                MATCH (a:File {path: r.src})
                MATCH (b:File {path: r.dst})
                MERGE (a)-[rel:IMPORTS]->(b)
                SET rel.specifier = r.specifier, rel.type_only = r.type_only
            """, imports_file)
            stats.edges[IMPORTS] = stats.edges.get(IMPORTS, 0) + len(imports_file)

            _run_batched(s, """
                UNWIND $rows AS r
                MATCH (a:File {path: r.src})
                MATCH (b:External {specifier: r.spec})
                MERGE (a)-[rel:IMPORTS_EXTERNAL]->(b)
                SET rel.specifier = r.specifier, rel.type_only = r.type_only
            """, imports_ext)
            stats.edges["IMPORTS_EXTERNAL"] = len(imports_ext)

            _run_batched(s, """
                UNWIND $rows AS r
                MATCH (a:Class {id: r.src})
                MATCH (b:Class {id: r.dst})
                MERGE (a)-[:EXTENDS]->(b)
            """, extends)
            stats.edges[EXTENDS] = len(extends)

            _run_batched(s, """
                UNWIND $rows AS r
                MATCH (a:Class {id: r.src})
                MATCH (b:Class {id: r.dst})
                MERGE (a)-[:IMPLEMENTS]->(b)
            """, implements)
            stats.edges[IMPLEMENTS] = len(implements)

            _run_batched(s, """
                UNWIND $rows AS r
                MATCH (a:Class {id: r.src})
                MATCH (b:Class {id: r.dst})
                MERGE (a)-[:INJECTS]->(b)
            """, injects)
            stats.edges[INJECTS] = len(injects)

            _run_batched(s, """
                UNWIND $rows AS r
                MATCH (a:Function {id: r.src})
                MATCH (b:Function {id: r.dst})
                MERGE (a)-[:RENDERS]->(b)
            """, renders)
            stats.edges[RENDERS] = len(renders)

            _run_batched(s, """
                UNWIND $rows AS r
                MATCH (a:Function {id: r.src})
                MATCH (h:Hook {name: r.hook})
                MERGE (a)-[:USES_HOOK]->(h)
            """, uses_hook)
            stats.edges[USES_HOOK] = len(uses_hook)

            _run_batched(s, """
                UNWIND $rows AS r
                MATCH (a:Class {id: r.src})
                MATCH (d:Decorator {name: r.name})
                MERGE (a)-[:DECORATED_BY]->(d)
            """, decorated_class)
            stats.edges[DECORATED_BY] = len(decorated_class) + len(decorated_method)

        return stats


def _run_batched(session, cypher: str, rows: list[dict]) -> None:
    if not rows:
        return
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        session.run(cypher, rows=chunk)
