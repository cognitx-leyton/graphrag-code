This is a plain text document with no markdown headings.
It contains some prose about the project architecture but
does not use any heading markers. The parser should handle
this gracefully by returning a single untitled section.

The architecture follows a layered approach where parsers
produce typed nodes, the resolver links cross-file references,
and the loader writes everything to Neo4j in batched UNWIND
statements for performance.
