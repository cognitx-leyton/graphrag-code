# Example Cypher queries

Connect with:
- Browser: http://localhost:7475 (user `neo4j`, pass `codegraph123`)
- Bolt: `bolt://localhost:7688`
- Or via CLI: `codegraph query "<cypher>"`

## 1. List every HTTP endpoint with its controller

```cypher
MATCH (c:Class {is_controller:true})-[:EXPOSES]->(e:Endpoint)
RETURN c.name AS controller, e.method AS method, e.path AS path, e.handler AS handler
ORDER BY c.name, e.path
```

## 2. What does file X transitively depend on?

```cypher
MATCH (start:File {path:'packages/twenty-server/src/engine/core-modules/auth/controllers/google-auth.controller.ts'})
MATCH (start)-[:IMPORTS*1..3]->(d:File)
RETURN DISTINCT d.path AS dependency
ORDER BY dependency
```

## 3. Who depends on file X? (reverse)

```cypher
MATCH (d:File)-[:IMPORTS*1..3]->(start:File {path:'packages/twenty-server/src/engine/core-modules/auth/services/auth.service.ts'})
RETURN DISTINCT d.path AS dependent
ORDER BY dependent
```

## 4. Which services are most-injected (hubs of DI)?

```cypher
MATCH (svc:Class {is_injectable:true})<-[:INJECTS]-(caller:Class)
RETURN svc.name AS service, svc.file AS file, count(caller) AS injections
ORDER BY injections DESC LIMIT 20
```

## 5. Constructor-DI chain from a controller

```cypher
MATCH path = (c:Class {name:'GoogleAuthController'})-[:INJECTS*1..3]->(dep:Class)
RETURN [n IN nodes(path) | n.name] AS chain
LIMIT 20
```

## 6. React: which components use a given hook?

```cypher
MATCH (h:Hook {name:'useAuth'})<-[:USES_HOOK]-(c:Function)
RETURN c.name AS component, c.file AS file
ORDER BY file
```

## 7. Most-rendered components (core UI primitives)

```cypher
MATCH (c:Function {is_component:true})<-[:RENDERS]-(parent)
RETURN c.name AS component, count(parent) AS used_by
ORDER BY used_by DESC LIMIT 20
```

## 8. Shortest path between two files

```cypher
MATCH (a:File), (b:File)
WHERE a.path ENDS WITH 'google-auth.controller.ts'
  AND b.path ENDS WITH 'workspace.entity.ts'
MATCH p = shortestPath((a)-[:IMPORTS*..8]->(b))
RETURN [n IN nodes(p) | n.path] AS hops
```

## 9. Orphan files (possible parse failures or dead code)

```cypher
MATCH (f:File)
WHERE NOT (f)-[:IMPORTS|IMPORTS_EXTERNAL]->()
  AND NOT ()-[:IMPORTS]->(f)
RETURN f.path AS orphan ORDER BY f.path LIMIT 50
```

## 10. Endpoints under a URL prefix

```cypher
MATCH (e:Endpoint) WHERE e.path STARTS WITH '/auth'
RETURN e.method, e.path, e.handler, e.file
ORDER BY e.path
```
