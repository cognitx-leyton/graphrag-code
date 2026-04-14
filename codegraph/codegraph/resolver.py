"""Import resolution + two-pass reference linking.

Reads per-package tsconfig.json path aliases, resolves each raw import
specifier to a concrete FileNode in the index, and links name-based
references (extends/implements/DI/JSX/hook) to class/function nodes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .schema import (
    ClassNode,
    Edge,
    EXTENDS,
    FunctionNode,
    IMPLEMENTS,
    IMPORTS,
    INJECTS,
    ParseResult,
    RENDERS,
    USES_HOOK,
)

# File resolution candidates tried for each specifier
_EXT_CANDIDATES = [
    "",
    ".ts",
    ".tsx",
    ".d.ts",
    "/index.ts",
    "/index.tsx",
    "/index.d.ts",
]


# ── tsconfig path aliases ────────────────────────────────────

@dataclass
class PackageConfig:
    """Describes one package root and its alias map."""
    name: str                          # e.g. "twenty-server"
    root: Path                         # absolute path to package dir
    repo_root: Path                    # absolute repo root (for rel paths)
    # alias → list of absolute base dirs (like tsconfig paths["foo/*"])
    aliases: dict[str, list[Path]] = field(default_factory=dict)

    @property
    def rel_root(self) -> str:
        return str(self.root.relative_to(self.repo_root)).replace("\\", "/")


def load_package_config(repo_root: Path, package_dir: Path) -> PackageConfig:
    name = package_dir.name
    cfg = PackageConfig(name=name, root=package_dir.resolve(), repo_root=repo_root.resolve())
    tsconfig = package_dir / "tsconfig.json"
    paths = _read_ts_paths(tsconfig)
    for key, targets in paths.items():
        # Normalize "foo/*" → "foo/"
        alias_prefix = key.rstrip("*")
        resolved_targets: list[Path] = []
        for t in targets:
            tp = (package_dir / t.rstrip("*")).resolve()
            resolved_targets.append(tp)
        cfg.aliases[alias_prefix] = resolved_targets
    return cfg


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_jsonc(raw: str) -> str:
    """Remove // and /* */ comments, honoring string literals."""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == '"':
            # copy string literal verbatim, handle escapes
            out.append(ch)
            i += 1
            while i < n:
                c = raw[i]
                out.append(c)
                i += 1
                if c == "\\" and i < n:
                    out.append(raw[i])
                    i += 1
                elif c == '"':
                    break
            continue
        if ch == "/" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "/":
                # line comment
                i += 2
                while i < n and raw[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                # block comment
                i += 2
                while i + 1 < n and not (raw[i] == "*" and raw[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _read_ts_paths(tsconfig: Path) -> dict[str, list[str]]:
    """Extract compilerOptions.paths, tolerating JSONC."""
    if not tsconfig.exists():
        return {}
    try:
        raw = tsconfig.read_text()
    except OSError:
        return {}
    cleaned = _strip_jsonc(raw)
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    return (data.get("compilerOptions") or {}).get("paths") or {}


# ── Index ────────────────────────────────────────────────────

@dataclass
class Index:
    """Repo-wide index built from parse results."""
    files_by_path: dict[str, ParseResult] = field(default_factory=dict)
    class_by_name_in_file: dict[tuple[str, str], ClassNode] = field(default_factory=dict)
    func_by_name_in_file: dict[tuple[str, str], FunctionNode] = field(default_factory=dict)
    # name → list of files that export a class/function with that name
    class_name_to_files: dict[str, list[str]] = field(default_factory=dict)
    func_name_to_files: dict[str, list[str]] = field(default_factory=dict)

    def add(self, result: ParseResult) -> None:
        path = result.file.path
        self.files_by_path[path] = result
        for c in result.classes:
            self.class_by_name_in_file[(path, c.name)] = c
            self.class_name_to_files.setdefault(c.name, []).append(path)
        for f in result.functions:
            self.func_by_name_in_file[(path, f.name)] = f
            self.func_name_to_files.setdefault(f.name, []).append(path)


# ── Specifier resolution ─────────────────────────────────────

class Resolver:
    def __init__(self, repo_root: Path, packages: list[PackageConfig]) -> None:
        self.repo_root = repo_root.resolve()
        self.packages = packages
        # path strings available in the index, populated after parse
        self._known_files: set[str] = set()

    def set_known_files(self, paths: set[str]) -> None:
        self._known_files = paths

    def resolve(self, importer_rel: str, specifier: str) -> Optional[str]:
        """Return the resolved repo-relative path, or None for external/unresolved."""
        spec = specifier.strip()
        if not spec:
            return None
        # Skip obvious externals
        if spec.startswith("@") and "/" in spec and not self._is_alias(spec):
            return None
        if not spec.startswith(".") and not spec.startswith("/") and not self._is_alias(spec):
            # bare package (lodash, react, @nestjs/common, …)
            return None

        importer_abs = (self.repo_root / importer_rel).resolve()
        importer_dir = importer_abs.parent

        # 1. Relative
        if spec.startswith("."):
            return self._try_candidates((importer_dir / spec).resolve().parent, Path(spec).name)

        # 2. Alias
        for pkg in self.packages:
            hit = self._try_alias(pkg, spec)
            if hit is not None:
                return hit

        return None

    def _is_alias(self, spec: str) -> bool:
        for pkg in self.packages:
            for alias in pkg.aliases.keys():
                if spec.startswith(alias):
                    return True
        return False

    def _try_alias(self, pkg: PackageConfig, spec: str) -> Optional[str]:
        for alias, targets in pkg.aliases.items():
            if not spec.startswith(alias):
                continue
            rest = spec[len(alias):]
            for t in targets:
                candidate = (t / rest).resolve() if rest else t
                hit = self._try_candidates(candidate.parent, candidate.name)
                if hit:
                    return hit
        return None

    def _try_candidates(self, directory: Path, stem: str) -> Optional[str]:
        base = (directory / stem).resolve()
        # Guard against escaping repo root
        try:
            base.relative_to(self.repo_root)
        except ValueError:
            return None
        for ext in _EXT_CANDIDATES:
            cand = Path(str(base) + ext)
            try:
                rel = str(cand.resolve().relative_to(self.repo_root)).replace("\\", "/")
            except ValueError:
                continue
            if rel in self._known_files:
                return rel
        return None


# ── Cross-file linker ────────────────────────────────────────

def link_cross_file(index: Index, resolver: Resolver) -> list[Edge]:
    """Emit IMPORTS / EXTENDS / IMPLEMENTS / INJECTS / RENDERS / USES_HOOK edges."""
    edges: list[Edge] = []
    resolver.set_known_files(set(index.files_by_path.keys()))

    unresolved_count = 0
    total_imports = 0

    for rel, result in index.files_by_path.items():
        # imports
        for spec, is_type in result.imports:
            total_imports += 1
            target = resolver.resolve(rel, spec)
            if target is not None:
                edges.append(Edge(
                    kind=IMPORTS,
                    src_id=f"file:{rel}",
                    dst_id=f"file:{target}",
                    props={"specifier": spec, "type_only": is_type},
                ))
            else:
                unresolved_count += 1
                edges.append(Edge(
                    kind=IMPORTS,
                    src_id=f"file:{rel}",
                    dst_id=f"external:{spec}",
                    props={"specifier": spec, "type_only": is_type, "external": True},
                ))

        # class_extends — try to resolve name to a class in a file that this file imports
        for cls_name, parent_name in result.class_extends:
            target = _find_class_for_name(rel, parent_name, index, resolver)
            if target is not None:
                edges.append(Edge(
                    kind=EXTENDS,
                    src_id=f"class:{rel}#{cls_name}",
                    dst_id=f"class:{target}#{parent_name}",
                ))

        for cls_name, iface_name in result.class_implements:
            target = _find_class_for_name(rel, iface_name, index, resolver)
            if target is not None:
                edges.append(Edge(
                    kind=IMPLEMENTS,
                    src_id=f"class:{rel}#{cls_name}",
                    dst_id=f"class:{target}#{iface_name}",
                ))

        # DI refs
        for cls_name, injected in result.di_refs:
            target = _find_class_for_name(rel, injected, index, resolver)
            if target is not None:
                edges.append(Edge(
                    kind=INJECTS,
                    src_id=f"class:{rel}#{cls_name}",
                    dst_id=f"class:{target}#{injected}",
                ))

        # JSX renders
        for component_name, rendered in result.jsx_renders:
            target = _find_func_for_name(rel, rendered, index, resolver)
            if target is not None:
                edges.append(Edge(
                    kind=RENDERS,
                    src_id=f"func:{rel}#{component_name}",
                    dst_id=f"func:{target}#{rendered}",
                ))

        # Hook calls — we emit edges to a "hook:<name>" node (string key)
        for component_name, hook in result.hook_calls:
            edges.append(Edge(
                kind=USES_HOOK,
                src_id=f"func:{rel}#{component_name}",
                dst_id=f"hook:{hook}",
                props={"hook": hook},
            ))

    edges.append(Edge(
        kind="__STATS__",
        src_id="",
        dst_id="",
        props={"total_imports": total_imports, "unresolved_imports": unresolved_count},
    ))
    return edges


def _find_class_for_name(
    importer: str,
    symbol: str,
    index: Index,
    resolver: Resolver,
) -> Optional[str]:
    """Look up which imported file defines a class named `symbol`."""
    result = index.files_by_path.get(importer)
    if result is None:
        return None
    # Prefer files that are actually imported by the importer
    for spec, _ in result.imports:
        target_path = resolver.resolve(importer, spec)
        if target_path is None:
            continue
        if (target_path, symbol) in index.class_by_name_in_file:
            return target_path
    # Fallback: single global definition
    files = index.class_name_to_files.get(symbol, [])
    if len(files) == 1:
        return files[0]
    return None


def _find_func_for_name(
    importer: str,
    symbol: str,
    index: Index,
    resolver: Resolver,
) -> Optional[str]:
    result = index.files_by_path.get(importer)
    if result is None:
        return None
    for spec, _ in result.imports:
        target_path = resolver.resolve(importer, spec)
        if target_path is None:
            continue
        if (target_path, symbol) in index.func_by_name_in_file:
            return target_path
    files = index.func_name_to_files.get(symbol, [])
    if len(files) == 1:
        return files[0]
    return None
