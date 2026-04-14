"""Static dependency slicing for TS/Java backends + frontend pairing.

Builds small feature-focused file slices from uploaded source files
and pairs backend slices with their corresponding frontend code
for end-to-end traceability.

Supported backend frameworks:
  - TypeScript: Fastify, Express (import/require, app.get())
  - Java: Spring Boot (@RestController, @GetMapping, import statements)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional


# ── Regex patterns (backend) ─────────────────────────────────

_IMPORT_RE = re.compile(
    r"import\s+(?:type\s+)?(?P<what>[\s\S]*?)\s+from\s+['\"](?P<module>[^'\"]+)['\"]",
    re.MULTILINE,
)
_REQUIRE_RE = re.compile(r"require\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\)")
_ROUTE_REG_RE = re.compile(
    r"\b(?:app|server|fastify)\.(?:use|register)\(\s*(?P<ident>[A-Za-z_]\w*)",
    re.MULTILINE,
)
_ENDPOINT_RE = re.compile(
    r"\b(?:router|app|fastify)\.(?:get|post|put|patch|delete|options|head)\s*\(",
    re.IGNORECASE,
)

# Extract HTTP method + URL path from route handler definitions
_ENDPOINT_DEF_RE = re.compile(
    r"""\b(?:app|fastify|server|router)\."""
    r"""(?P<method>get|post|put|patch|delete|options|head)\s*\(\s*"""
    r"""(?:['"](?P<path1>[^'"]+)['"]|`(?P<path2>[^`]+)`)""",
    re.IGNORECASE | re.MULTILINE,
)

# ── Regex patterns (Java / Spring Boot) ──────────────────────

# Java imports: import com.ecommerce.catalogue.model.Product;
_JAVA_IMPORT_RE = re.compile(
    r"import\s+(?:static\s+)?(?P<module>[\w.]+)\s*;",
    re.MULTILINE,
)

# Spring Boot endpoint annotations (handles @GetMapping, @GetMapping(), @GetMapping("/path"))
_SPRING_ENDPOINT_RE = re.compile(
    r"@(?P<ann>GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping"
    r"|RequestMapping)"
    r"(?:\s*\(\s*"
    r"(?:value\s*=\s*)?"
    r"""(?:['""](?P<path1>[^'""]*)['""])?"""
    r"\s*\))?",
    re.IGNORECASE | re.MULTILINE,
)

# @RequestMapping on class level (base path)
_SPRING_CLASS_MAPPING_RE = re.compile(
    r"""@RequestMapping\s*\(\s*(?:value\s*=\s*)?['"](?P<path>[^'"]+)['"]""",
    re.MULTILINE,
)

# Spring controller annotations
_SPRING_CONTROLLER_RE = re.compile(
    r"@(?:RestController|Controller)\b",
    re.MULTILINE,
)

# ── Regex patterns (PHP / Laravel) ─────────────────────────────

# PHP use statements: use App\Http\Controllers\FooController;
_PHP_USE_RE = re.compile(
    r"use\s+(?P<module>[A-Z][\w\\]+)\s*;",
    re.MULTILINE,
)

# PHP require/include: require __DIR__ . '/auth.php'; or require_once 'file.php';
_PHP_REQUIRE_RE = re.compile(
    r"(?:require|include)(?:_once)?\s+(?:__DIR__\s*\.\s*)?['\"](?P<path>[^'\"]+)['\"]",
    re.MULTILINE,
)

# Laravel route definitions: Route::get('/path', ...)
_LARAVEL_ROUTE_RE = re.compile(
    r"""Route\s*::\s*(?P<method>get|post|put|patch|delete|options|any|match|resource|apiResource)\s*\(\s*"""
    r"""['"](?P<path>[^'"]+)['"]""",
    re.IGNORECASE | re.MULTILINE,
)

# ── Regex patterns (Python / Odoo / Django / Flask) ──────────

# Python from-import: from odoo import models, fields
_PYTHON_FROM_IMPORT_RE = re.compile(
    r"from\s+(?P<module>[\w.]+)\s+import",
    re.MULTILINE,
)

# Python plain import: import odoo.addons.sale.models
_PYTHON_IMPORT_RE = re.compile(
    r"^import\s+(?P<module>[\w.]+)",
    re.MULTILINE,
)

# Odoo controller class: class MyController(http.Controller):
_ODOO_CONTROLLER_RE = re.compile(
    r"class\s+\w+\s*\([^)]*(?:http\.Controller|Controller)\s*[^)]*\)",
    re.MULTILINE,
)

# Odoo model class: class Partner(models.Model): or class ProductWizard(models.TransientModel):
_ODOO_MODEL_RE = re.compile(
    r"class\s+\w+\s*\([^)]*models\.(?:Model|TransientModel|AbstractModel)\s*[^)]*\)",
    re.MULTILINE,
)

# Odoo route decorator: @http.route('/path', ...)
_ODOO_ROUTE_RE = re.compile(
    r"""@(?:http\.)?route\s*\(\s*"""
    r"""(?:\[\s*)?['"](?P<path>[^'"]+)['"]"""
    r"""(?:[^)]*type\s*=\s*['"](?P<type>[^'"]+)['"])?""",
    re.MULTILINE,
)

# Django urlpatterns: path('endpoint/', view_func)
_DJANGO_PATH_RE = re.compile(
    r"""(?:path|re_path)\s*\(\s*['"](?P<path>[^'"]+)['"]""",
    re.MULTILINE,
)

# Flask route: @app.route('/path', methods=['GET'])
_FLASK_ROUTE_RE = re.compile(
    r"""@\w+\.route\s*\(\s*['"](?P<path>[^'"]+)['"]"""
    r"""(?:[^)]*methods\s*=\s*\[\s*['"](?P<method>[^'"]+)['"])?""",
    re.MULTILINE,
)

# ── Regex patterns (frontend) ────────────────────────────────

# apiClient.get('/path') or apiClient.get<T>(`/path/${id}`)
_APICLIENT_CALL_RE = re.compile(
    r"""apiClient\s*\.\s*(?P<method>get|post|put|patch|delete)\s*"""
    r"""(?:<[^>]*>)?\s*\(\s*"""
    r"""(?:['"](?P<path1>[^'"]+)['"]|`(?P<path2>[^`]+)`)""",
    re.IGNORECASE | re.MULTILINE,
)

# Angular HttpClient: this.http.get('/path'), this.httpClient.post<T>(`/path`)
_HTTP_CLIENT_CALL_RE = re.compile(
    r"""(?:this\.http|this\.httpClient|this\.client)\s*\.\s*"""
    r"""(?P<method>get|post|put|patch|delete|request)\s*"""
    r"""(?:<[^>]*>)?\s*\(\s*"""
    r"""(?:['"](?P<path1>[^'"]+)['"]|`(?P<path2>[^`]+)`)""",
    re.IGNORECASE | re.MULTILINE,
)

# fetch('/path') or fetch(`/path/${id}`)
_FETCH_CALL_RE = re.compile(
    r"""\bfetch\s*\(\s*"""
    r"""(?:['"](?P<path1>[^'"]+)['"]|`(?P<path2>[^`]+)`)""",
    re.IGNORECASE | re.MULTILINE,
)

# axios.get('/path'), axios.post('/path', data), axios('/path')
_AXIOS_CALL_RE = re.compile(
    r"""\baxios\s*(?:\.\s*(?P<method>get|post|put|patch|delete))?\s*"""
    r"""(?:<[^>]*>)?\s*\(\s*"""
    r"""(?:['"](?P<path1>[^'"]+)['"]|`(?P<path2>[^`]+)`)""",
    re.IGNORECASE | re.MULTILINE,
)

# Generic: any string literal that looks like an API path.
# Catches custom wrappers like fetchJson('/api/...'), axios.get('/api/...'), etc.
_API_PATH_LITERAL_RE = re.compile(
    r"""['"`](?P<path>/(?:api|v\d+)/[^'"`\s]+?)['"`]""",
    re.MULTILINE,
)

# Template literal with env variable prefix: `${environment.API_URL}/auth/login`
# Extracts the path portion after the variable interpolation.
_ENV_TEMPLATE_URL_RE = re.compile(
    r"""`\$\{[^}]*(?:API_URL|apiUrl|BASE_URL|baseUrl|API_BASE|apiBase)[^}]*\}(?P<path>/[^`]+)`""",
    re.IGNORECASE | re.MULTILINE,
)

# String concatenation with env variable: environment.API_URL + '/auth/login'
_ENV_CONCAT_URL_RE = re.compile(
    r"""(?:API_URL|apiUrl|BASE_URL|baseUrl|API_BASE|apiBase)\s*\+\s*['"](?P<path>/[^'"]+)['"]""",
    re.IGNORECASE | re.MULTILINE,
)

# supabase.from('table_name')
_SUPABASE_FROM_RE = re.compile(
    r"""\.from\s*\(\s*['"](?P<table>\w+)['"]""",
    re.IGNORECASE,
)


# ── Data classes ─────────────────────────────────────────────

@dataclass
class Endpoint:
    """A single HTTP endpoint extracted from a backend route file."""
    method: str  # GET, POST, PUT, DELETE, PATCH
    path: str    # /records, /:id, /stages/:stageId


@dataclass
class DependencySlice:
    """A feature-focused slice of backend files."""
    entrypoint: str
    feature_name: str
    files: dict[str, str]
    endpoints: list[Endpoint] = field(default_factory=list)
    estimated_tokens: int = 0


@dataclass
class FrontendSlice:
    """A slice of frontend files corresponding to a backend feature."""
    feature_name: str
    files: dict[str, str]
    entry_hooks: list[str]    # The hook/service files that triggered the match
    api_urls: list[str] = field(default_factory=list)
    estimated_tokens: int = 0


@dataclass
class FeaturePair:
    """A paired backend slice + frontend slice for a single feature."""
    feature_name: str
    backend: DependencySlice
    frontend: Optional[FrontendSlice]
    estimated_tokens: int = 0


@dataclass
class GraphData:
    """Serializable dependency graph for visualization."""
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    slices: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"nodes": self.nodes, "edges": self.edges, "slices": self.slices}


# ── Utility functions ────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token)."""
    return max(1, len(text) // 4)


def _norm_path(path: str) -> str:
    path = path.replace("\\", "/")
    return str(PurePosixPath(path))


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


# ── Import resolution ────────────────────────────────────────

def _extract_imports(file_path: str, content: str, path_index: set[str]) -> list[str]:
    imports: list[str] = []

    # TypeScript / JavaScript imports
    for match in _IMPORT_RE.finditer(content):
        module = match.group("module").strip()
        resolved = _resolve_local_import(file_path, module, path_index)
        if not resolved:
            resolved = _resolve_aliased_import(module, path_index)
        if resolved:
            imports.append(resolved)
    for match in _REQUIRE_RE.finditer(content):
        module = match.group("module").strip()
        resolved = _resolve_local_import(file_path, module, path_index)
        if not resolved:
            resolved = _resolve_aliased_import(module, path_index)
        if resolved:
            imports.append(resolved)

    # Java imports: import com.ecommerce.catalogue.model.Product;
    if file_path.lower().endswith(".java"):
        for match in _JAVA_IMPORT_RE.finditer(content):
            module = match.group("module").strip()
            resolved = _resolve_java_import(module, path_index)
            if resolved:
                imports.append(resolved)

    # PHP use statements: use App\Http\Controllers\FooController;
    if file_path.lower().endswith(".php"):
        for match in _PHP_USE_RE.finditer(content):
            module = match.group("module").strip()
            resolved = _resolve_php_use(module, path_index)
            if resolved:
                imports.append(resolved)
        # PHP require/include
        for match in _PHP_REQUIRE_RE.finditer(content):
            req_path = match.group("path").strip()
            resolved = _resolve_php_require(file_path, req_path, path_index)
            if resolved:
                imports.append(resolved)

    # Python imports: from X import Y / import X
    if file_path.lower().endswith(".py"):
        for match in _PYTHON_FROM_IMPORT_RE.finditer(content):
            module = match.group("module").strip()
            resolved = _resolve_python_import(file_path, module, path_index)
            if resolved:
                imports.append(resolved)
        for match in _PYTHON_IMPORT_RE.finditer(content):
            module = match.group("module").strip()
            resolved = _resolve_python_import(file_path, module, path_index)
            if resolved:
                imports.append(resolved)

    return _dedupe_preserve_order(imports)


def _resolve_local_import(file_path: str, module_ref: str, path_index: set[str]) -> Optional[str]:
    if not module_ref.startswith("."):
        return None
    base = PurePosixPath(file_path).parent
    candidate = _norm_path(str(base / module_ref))
    no_ext = candidate[:-3] if candidate.endswith(".ts") else candidate
    candidates = [
        candidate,
        f"{no_ext}.ts",
        f"{no_ext}.tsx",
        f"{no_ext}.js",
        f"{no_ext}.jsx",
        f"{no_ext}.java",
        f"{candidate}/index.ts",
        f"{candidate}/index.tsx",
    ]
    for cand in candidates:
        if cand in path_index:
            return cand
    return None


def _resolve_aliased_import(module_ref: str, path_index: set[str]) -> Optional[str]:
    """Resolve @/ aliased imports (common in React/Vite projects)."""
    if not module_ref.startswith("@/"):
        return None
    relative = module_ref[2:]  # Remove @/
    for prefix in ("src/", ""):
        candidate = _norm_path(f"{prefix}{relative}")
        candidates = [
            candidate,
            f"{candidate}.ts",
            f"{candidate}.tsx",
            f"{candidate}.js",
            f"{candidate}.jsx",
            f"{candidate}/index.ts",
            f"{candidate}/index.tsx",
        ]
        for cand in candidates:
            if cand in path_index:
                return cand
    return None


def _resolve_java_import(module_ref: str, path_index: set[str]) -> Optional[str]:
    """Resolve a Java import like com.ecommerce.catalogue.model.Product to a file path.

    Converts dotted package name to path: com.ecommerce.catalogue.model.Product
    → com/ecommerce/catalogue/model/Product.java
    Then searches path_index with various prefix combinations.
    """
    # Skip standard library / third-party imports
    if module_ref.startswith(("java.", "javax.", "jakarta.", "org.springframework.",
                              "org.apache.", "org.slf4j.", "lombok.", "org.junit.",
                              "org.mockito.", "com.fasterxml.", "io.swagger.")):
        return None

    # Convert dots to path separators
    parts = module_ref.replace(".", "/")
    candidate = f"{parts}.java"

    # Try exact match and with common prefixes
    prefixes = ["", "src/main/java/", "src/"]
    for prefix in prefixes:
        full = _norm_path(f"{prefix}{candidate}")
        if full in path_index:
            return full
        # Also try without the last segment (might be an inner class)
        parent = "/".join(parts.split("/")[:-1])
        if parent:
            parent_file = _norm_path(f"{prefix}{parent}.java")
            if parent_file in path_index:
                return parent_file

    # Fuzzy: search for any file ending with the class name
    class_name = module_ref.rsplit(".", 1)[-1]
    target = f"/{class_name}.java"
    for path in sorted(path_index):
        if path.endswith(target):
            return path

    return None


def _resolve_php_use(module_ref: str, path_index: set[str]) -> Optional[str]:
    """Resolve a PHP use statement like App\\Http\\Controllers\\FooController to a file path.

    Converts backslash namespace to forward-slash path:
    App\\Http\\Controllers\\FooController → app/Http/Controllers/FooController.php
    """
    # Convert namespace to path
    parts = module_ref.replace("\\", "/")
    candidate = f"{parts}.php"

    # Try with various common prefixes
    prefixes = ["", "app/", "src/"]
    for prefix in prefixes:
        full = _norm_path(f"{prefix}{candidate}")
        if full in path_index:
            return full
        # Also try lowercase first segment (App → app)
        lower_first = parts[0].lower() + parts[1:] if parts else parts
        full_lower = _norm_path(f"{prefix}{lower_first}.php")
        if full_lower in path_index:
            return full_lower

    # Fuzzy: search for any file ending with the class name
    class_name = module_ref.rsplit("\\", 1)[-1]
    target = f"/{class_name}.php"
    for path in sorted(path_index):
        if path.endswith(target):
            return path

    return None


def _resolve_php_require(file_path: str, require_path: str, path_index: set[str]) -> Optional[str]:
    """Resolve a PHP require/include relative path."""
    base = PurePosixPath(file_path).parent
    # Handle paths starting with /
    if require_path.startswith("/"):
        require_path = require_path[1:]
    candidate = _norm_path(str(base / require_path))
    if candidate in path_index:
        return candidate
    # Try from project root
    root_candidate = _norm_path(require_path)
    if root_candidate in path_index:
        return root_candidate
    return None


def _resolve_python_import(file_path: str, module_ref: str, path_index: set[str]) -> Optional[str]:
    """Resolve a Python import like 'from .models import Product' or 'from odoo.addons.sale.models'.

    Handles:
    - Relative imports: from .models import X → same_dir/models.py
    - Absolute within project: from mymodule.models import X → mymodule/models.py
    - Odoo addons: from odoo.addons.sale.models → sale/models.py
    """
    # Skip stdlib / third-party imports
    if module_ref.startswith((
        "os", "sys", "re", "json", "logging", "datetime", "collections",
        "typing", "abc", "functools", "itertools", "pathlib", "hashlib",
        "copy", "math", "io", "contextlib", "textwrap", "uuid", "base64",
        "urllib", "http", "email", "unittest", "pdb", "traceback",
        "werkzeug", "flask", "django", "requests", "numpy", "pandas",
        "psycopg2", "sqlalchemy", "celery", "redis", "boto3",
        "PIL", "lxml", "bs4", "pydantic", "fastapi",
    )):
        return None

    # Handle Odoo addons: from odoo.addons.module_name.models import X
    if module_ref.startswith("odoo.addons."):
        parts = module_ref[len("odoo.addons."):].replace(".", "/")
        candidate = f"{parts}.py"
        for path in sorted(path_index):
            if path.endswith(candidate) or path.endswith(f"{parts}/__init__.py"):
                return path
        return None

    # Skip bare odoo imports (framework, not project code)
    if module_ref.startswith("odoo.") or module_ref == "odoo":
        return None

    # Relative import: module_ref starts with '.'
    if module_ref.startswith("."):
        base = PurePosixPath(file_path).parent
        # Count leading dots for relative depth
        dots = len(module_ref) - len(module_ref.lstrip("."))
        remainder = module_ref.lstrip(".")
        for _ in range(dots - 1):
            base = base.parent
        if remainder:
            candidate = _norm_path(str(base / remainder.replace(".", "/")))
        else:
            candidate = _norm_path(str(base))
        candidates = [
            f"{candidate}.py",
            f"{candidate}/__init__.py",
        ]
        for cand in candidates:
            if cand in path_index:
                return cand
        return None

    # Absolute project import: convert dots to slashes
    parts = module_ref.replace(".", "/")
    candidates = [
        f"{parts}.py",
        f"{parts}/__init__.py",
    ]
    for cand in candidates:
        norm = _norm_path(cand)
        if norm in path_index:
            return norm

    # Fuzzy: search for file ending with the last segment
    last_segment = module_ref.rsplit(".", 1)[-1]
    target = f"/{last_segment}.py"
    for path in sorted(path_index):
        if path.endswith(target):
            return path

    return None


def _extract_imported_symbols(raw: str) -> list[str]:
    symbols: list[str] = []
    cleaned = raw.strip()
    if not cleaned:
        return symbols
    if "{" in cleaned and "}" in cleaned:
        inner = cleaned[cleaned.find("{") + 1 : cleaned.rfind("}")]
        for part in inner.split(","):
            name = part.strip()
            if not name:
                continue
            symbols.append(name.split(" as ")[-1].strip())
    else:
        top = cleaned.split(",")[0].strip()
        if top and top != "*":
            symbols.append(top)
    return symbols


def _import_symbol_map(
    file_path: str,
    content: str,
    resolved_imports: list[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    import_iter = list(_IMPORT_RE.finditer(content))
    for i, match in enumerate(import_iter):
        what = match.group("what")
        names = _extract_imported_symbols(what)
        if i < len(resolved_imports):
            target = resolved_imports[i]
            for name in names:
                result[name] = target
    return result


# ── Backend slicing ──────────────────────────────────────────

def build_dependency_slices(files: dict[str, str]) -> list[DependencySlice]:
    """Build feature slices by tracing local imports from route entry points."""
    if not files:
        return []

    normalized = {
        _norm_path(path): files[path]
        for path in sorted(files.keys())
        if not _should_exclude(_norm_path(path), files[path])
    }
    path_index = set(normalized.keys())
    imports_by_file = {
        path: _extract_imports(path, normalized[path], path_index)
        for path in sorted(normalized.keys())
    }

    explicit_entries = _find_registered_routes(normalized, imports_by_file)
    route_candidates = _find_route_candidates(normalized)
    entrypoints = _dedupe_preserve_order(explicit_entries + route_candidates)
    if not entrypoints:
        entrypoints = _fallback_entrypoints(normalized)

    raw_slices: list[DependencySlice] = []
    for entry in entrypoints:
        traced = _trace_dependencies(entry, imports_by_file, normalized)
        if not traced:
            continue
        slice_files = {path: normalized[path] for path in traced}
        all_text = "\n".join(slice_files.values())

        # Extract endpoints from all files in the slice
        endpoints: list[Endpoint] = []
        for path in traced:
            endpoints.extend(_extract_endpoints_from_content(normalized[path]))

        raw_slices.append(
            DependencySlice(
                entrypoint=entry,
                feature_name=_infer_feature_name(entry),
                files=slice_files,
                endpoints=endpoints,
                estimated_tokens=estimate_tokens(all_text),
            )
        )

    # ── Merge slices that share the same feature_name ────────────
    merged_map: dict[str, DependencySlice] = {}
    for sl in raw_slices:
        fname = sl.feature_name
        if fname in merged_map:
            existing = merged_map[fname]
            existing.files.update(sl.files)
            # Merge endpoints (deduplicate by method+path)
            seen_eps = {(e.method, e.path) for e in existing.endpoints}
            for ep in sl.endpoints:
                if (ep.method, ep.path) not in seen_eps:
                    existing.endpoints.append(ep)
                    seen_eps.add((ep.method, ep.path))
            # Recompute token estimate
            existing.estimated_tokens = estimate_tokens("\n".join(existing.files.values()))
        else:
            merged_map[fname] = sl

    return list(merged_map.values())


def _find_registered_routes(
    files: dict[str, str],
    imports_by_file: dict[str, list[str]],
) -> list[str]:
    entries: list[str] = []

    # TypeScript: look in app.ts / main.ts for app.register(handler)
    for path in sorted(files.keys()):
        content = files[path]
        lower = path.lower()
        if not lower.endswith(".ts"):
            continue
        if not any(name in lower for name in ("app.ts", "main.ts", "server.ts", "index.ts")):
            continue

        symbol_map = _import_symbol_map(path, content, imports_by_file.get(path, []))
        for match in _ROUTE_REG_RE.finditer(content):
            ident = match.group("ident")
            target = symbol_map.get(ident)
            if target and _looks_like_route_file(target, files.get(target, "")):
                entries.append(target)

    # Java: look for @RestController / @Controller annotated classes
    _JAVA_SKIP_NAMES = {"package-info.java"}
    _JAVA_SKIP_PATTERNS = re.compile(
        r"(?:Config|Configuration|Interceptor|Aspect|Validator|Filter|Handler|"
        r"Advice|Initializer|Application)\s*\.java$",
        re.IGNORECASE,
    )
    for path in sorted(files.keys()):
        content = files[path]
        if not path.lower().endswith(".java"):
            continue
        filename = path.rsplit("/", 1)[-1]
        # Skip metadata and infrastructure files
        if filename.lower() in _JAVA_SKIP_NAMES:
            continue
        if _JAVA_SKIP_PATTERNS.search(filename):
            continue
        if _SPRING_CONTROLLER_RE.search(content):
            entries.append(path)

    # PHP / Laravel: route files and controller classes
    for path in sorted(files.keys()):
        content = files[path]
        if not path.lower().endswith(".php"):
            continue
        if "/routes/" in path.lower() and _LARAVEL_ROUTE_RE.search(content):
            entries.append(path)
        elif path.lower().endswith("controller.php"):
            entries.append(path)

    # Python / Odoo: look for http.Controller classes and @http.route
    for path in sorted(files.keys()):
        content = files[path]
        if not path.lower().endswith(".py"):
            continue
        if _ODOO_CONTROLLER_RE.search(content) or _ODOO_ROUTE_RE.search(content):
            entries.append(path)

    # Python / Odoo: treat each model file as a feature entrypoint
    # In Odoo, models define the business logic (fields, compute, constraints, CRUD).
    # Skip infrastructure models that just customize Odoo internals.
    _ODOO_INFRA_PREFIXES = (
        "ir_",          # ir.rule, ir.ui.view, ir.actions.*, ir.default, ir.exports...
        "base",         # base.py, base_automation.py, base_translatable_name.py
        "fields",       # fields.py (field utilities)
        "mapping_",     # mapping helpers
        "auth_",        # auth_oauth.py (SSO plumbing)
        "mail_",        # mail_thread.py, mail_mail.py, mail_log.py (messaging infra)
        "email_",       # email_template.py
    )
    model_entries: list[str] = []
    for path in sorted(files.keys()):
        content = files[path]
        lower = path.lower()
        if not lower.endswith(".py"):
            continue
        name = PurePosixPath(path).name.lower()
        if name == "__init__.py":
            continue
        # Skip Odoo infrastructure models
        if any(name.startswith(prefix) for prefix in _ODOO_INFRA_PREFIXES):
            continue
        if ("/models/" in lower or "/wizards/" in lower) and _ODOO_MODEL_RE.search(content):
            model_entries.append(path)
    # Cap model entrypoints to avoid excessive API calls on huge projects
    if len(model_entries) > 50:
        print(f"[slicer] Capping Odoo model entrypoints from {len(model_entries)} to 50")
        model_entries = model_entries[:50]
    entries.extend(model_entries)

    # Python / Django: look for urlpatterns
    for path in sorted(files.keys()):
        content = files[path]
        if not path.lower().endswith(".py"):
            continue
        lower = path.lower()
        if ("urls" in lower or "views" in lower) and _DJANGO_PATH_RE.search(content):
            entries.append(path)

    # Python / Flask: look for @app.route
    for path in sorted(files.keys()):
        content = files[path]
        if not path.lower().endswith(".py"):
            continue
        if _FLASK_ROUTE_RE.search(content):
            entries.append(path)

    return _dedupe_preserve_order(entries)


def _find_route_candidates(files: dict[str, str]) -> list[str]:
    candidates: list[str] = []
    for path in sorted(files.keys()):
        content = files[path]
        if _looks_like_route_file(path, content):
            candidates.append(path)
    return _dedupe_preserve_order(candidates)


def _looks_like_route_file(path: str, content: str) -> bool:
    lower = path.lower()
    return (
        # TypeScript / Express / Fastify
        "/routes/" in lower
        or lower.endswith(".routes.ts")
        or lower.endswith(".route.ts")
        or ("router" in lower and lower.endswith(".ts"))
        or bool(_ENDPOINT_RE.search(content))
        # Java / Spring Boot
        or "/controller/" in lower
        or lower.endswith("controller.java")
        or bool(_SPRING_CONTROLLER_RE.search(content))
        # PHP / Laravel
        or (lower.endswith(".php") and "/routes/" in lower)
        or (lower.endswith(".php") and bool(_LARAVEL_ROUTE_RE.search(content)))
        or (lower.endswith("controller.php"))
        # Python / Odoo (controllers only; models handled separately in _find_registered_routes)
        or (lower.endswith(".py") and bool(_ODOO_CONTROLLER_RE.search(content)))
        or (lower.endswith(".py") and bool(_ODOO_ROUTE_RE.search(content)))
        # Python / Django
        or (lower.endswith(".py") and ("urls" in lower or "views" in lower) and bool(_DJANGO_PATH_RE.search(content)))
        # Python / Flask
        or (lower.endswith(".py") and bool(_FLASK_ROUTE_RE.search(content)))
    )


def _fallback_entrypoints(files: dict[str, str]) -> list[str]:
    by_priority = sorted(files.keys(), key=lambda p: _entry_priority(p))
    return by_priority[:5]


def _entry_priority(path: str) -> int:
    lower = path.lower()
    if "/routes/" in lower or "route" in lower:
        return 0
    if "/controller/" in lower or lower.endswith("controller.java") or lower.endswith("controller.php"):
        return 0
    if "controller" in lower:
        return 1
    if "service" in lower:
        return 2
    if "views" in lower and lower.endswith(".py"):
        return 1
    if "model" in lower or "dto" in lower or "entity" in lower:
        return 3
    return 9


def _trace_dependencies(
    entrypoint: str,
    imports_by_file: dict[str, list[str]],
    files: dict[str, str],
) -> list[str]:
    queue = [entrypoint]
    seen: set[str] = set()
    included: list[str] = []
    max_files = 25

    while queue and len(included) < max_files:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if current not in files:
            continue

        content = files[current]
        if _should_exclude(current, content) and current != entrypoint:
            continue

        included.append(current)
        for dep in sorted(imports_by_file.get(current, [])):
            if dep not in seen:
                queue.append(dep)

    return included


def _should_exclude(path: str, content: str) -> bool:
    lower = path.lower()
    # Build / tooling directories
    if any(seg in lower for seg in (
        "node_modules", "/dist/", "/build/", "/coverage/", "/.git/",
        "/.next/", "/.turbo/", "/target/", "/.gradle/", "/.mvn/",
        "/__pycache__/", "/.idea/",
    )):
        return True
    # Test / spec / mock files
    if any(tok in lower for tok in (
        "test.", "spec.", "__tests__", ".config.", ".setup.", ".mock.",
    )):
        return True
    # TypeScript declaration files
    if lower.endswith(".d.ts"):
        return True
    # TS barrel re-exports
    if lower.endswith("/index.ts") and "export *" in content:
        return True
    # Java compiled classes
    if lower.endswith(".class"):
        return True
    # Config / non-code files
    if any(lower.endswith(ext) for ext in (
        ".xml", ".properties", ".yml", ".yaml", ".json", ".md",
        ".gradle", ".kts", ".lock",
    )):
        return True
    exclude_hints = ("logger", "swagger", "sentry")
    if any(tok in lower for tok in exclude_hints):
        return True
    # Python boilerplate / infrastructure files
    if lower.endswith(".py"):
        name = PurePosixPath(path).name.lower()
        if name in ("__init__.py", "__openerp__.py",
                    "setup.py", "manage.py", "conftest.py", "wsgi.py", "asgi.py"):
            return True
        if "/migrations/" in lower or "/migration/" in lower:
            return True
        # security/ CSV files are not code
        if "/security/" in lower and lower.endswith(".csv"):
            return True
    return False


def _infer_feature_name(path: str) -> str:
    name = PurePosixPath(path).name
    # Python __init__.py or TS index.ts → use parent directory name
    if name.lower() in ("__init__.py", "__manifest__.py") or name.lower().startswith("index."):
        parent_path = PurePosixPath(path).parent
        parent = parent_path.name
        # If the parent is a generic folder like "routes" or "controllers", go up one more level
        if parent.lower() in ("routes", "controllers", "services", "api"):
            parent = parent_path.parent.name
        if parent:
            name = parent
    # Strip known suffixes iteratively (handles workflow.routes.ts → workflow)
    for _ in range(4):
        prev = name
        name = re.sub(
            r"\.(routes?|controller|service|dto|entity|model|ts|tsx|js|jsx|java|py|go|php)$",
            "", name, flags=re.IGNORECASE,
        )
        # Java/PHP convention: ProductControllerImpl → Product
        name = re.sub(r"(Controller|Service|Repository|Application|Impl|Config)$", "", name)
        if name == prev:
            break
    name = name.replace("_", "-").replace(".", "-")
    # CamelCase → kebab-case (e.g., ProductController → product)
    name = re.sub(r"([a-z])([A-Z])", r"\1-\2", name).lower()
    return name or "feature"


def _extract_endpoints_from_content(content: str) -> list[Endpoint]:
    """Extract HTTP endpoints defined in a route/controller file."""
    endpoints: list[Endpoint] = []
    seen: set[tuple[str, str]] = set()

    # TypeScript: fastify.get("/path", ...)
    for match in _ENDPOINT_DEF_RE.finditer(content):
        method = match.group("method").upper()
        path = match.group("path1") or match.group("path2") or ""
        if path and not path.startswith("http") and (method, path) not in seen:
            seen.add((method, path))
            endpoints.append(Endpoint(method=method, path=path))

    # Java / Spring Boot: @GetMapping("/path"), @PostMapping("/path"), etc.
    # First, get base path from class-level @RequestMapping
    base_path = ""
    base_match = _SPRING_CLASS_MAPPING_RE.search(content)
    if base_match:
        base_path = base_match.group("path").rstrip("/")

    _ANNOTATION_TO_METHOD = {
        "getmapping": "GET", "postmapping": "POST", "putmapping": "PUT",
        "patchmapping": "PATCH", "deletemapping": "DELETE",
    }
    for match in _SPRING_ENDPOINT_RE.finditer(content):
        ann = match.group("ann").lower()
        path = match.group("path1") or ""
        method = _ANNOTATION_TO_METHOD.get(ann, "")
        if ann == "requestmapping":
            # Skip class-level mapping (already captured as base_path)
            if base_match and match.start() == base_match.start():
                continue
            method = "GET"  # Default for @RequestMapping
        full_path = f"{base_path}/{path.lstrip('/')}" if path else base_path or "/"
        if full_path and method and (method, full_path) not in seen:
            seen.add((method, full_path))
            endpoints.append(Endpoint(method=method, path=full_path))

    # PHP / Laravel: Route::get('/path', ...)
    _LARAVEL_METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "options": "OPTIONS", "any": "ANY",
        "resource": "RESOURCE", "apiresource": "RESOURCE",
    }
    for match in _LARAVEL_ROUTE_RE.finditer(content):
        method = _LARAVEL_METHOD_MAP.get(match.group("method").lower(), "GET")
        path = match.group("path")
        if path and (method, path) not in seen:
            seen.add((method, path))
            endpoints.append(Endpoint(method=method, path=path))

    # Python / Odoo: @http.route('/path', type='json')
    for match in _ODOO_ROUTE_RE.finditer(content):
        path = match.group("path")
        route_type = match.group("type") or "http"
        method = "JSON" if route_type == "json" else "GET"
        if path and (method, path) not in seen:
            seen.add((method, path))
            endpoints.append(Endpoint(method=method, path=path))

    # Python / Django: path('endpoint/', ...)
    for match in _DJANGO_PATH_RE.finditer(content):
        path = match.group("path")
        if path and ("GET", path) not in seen:
            seen.add(("GET", path))
            endpoints.append(Endpoint(method="GET", path=path))

    # Python / Flask: @app.route('/path', methods=['POST'])
    for match in _FLASK_ROUTE_RE.finditer(content):
        path = match.group("path")
        method = (match.group("method") or "GET").upper()
        if path and (method, path) not in seen:
            seen.add((method, path))
            endpoints.append(Endpoint(method=method, path=path))

    return endpoints


# ── Frontend analysis ────────────────────────────────────────

def _extract_api_urls(content: str) -> list[str]:
    """Extract API endpoint URLs referenced in frontend code.

    Detects patterns from: apiClient, Angular HttpClient, fetch, axios,
    environment-based URL templates, and generic /api/ string literals.
    """
    urls: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        if not path or path.startswith("http"):
            return
        # Clean template-literal expressions like ${id}
        cleaned = re.sub(r"\$\{[^}]*\}", "", path).rstrip("/")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)

    # Specific HTTP client patterns (high confidence)
    for regex in (_APICLIENT_CALL_RE, _HTTP_CLIENT_CALL_RE, _FETCH_CALL_RE, _AXIOS_CALL_RE):
        for match in regex.finditer(content):
            _add(match.group("path1") or match.group("path2") or "")

    # Environment-based URL templates: `${environment.API_URL}/auth/login`
    for match in _ENV_TEMPLATE_URL_RE.finditer(content):
        _add(match.group("path"))

    # String concatenation: environment.API_URL + '/auth/login'
    for match in _ENV_CONCAT_URL_RE.finditer(content):
        _add(match.group("path"))

    # Generic fallback: any string literal that looks like an API path
    for match in _API_PATH_LITERAL_RE.finditer(content):
        _add(match.group("path"))

    return urls


def _extract_supabase_tables(content: str) -> list[str]:
    """Extract Supabase table names from .from('table') calls."""
    tables: list[str] = []
    seen: set[str] = set()
    for match in _SUPABASE_FROM_RE.finditer(content):
        table = match.group("table")
        if table not in seen:
            seen.add(table)
            tables.append(table)
    return tables


def _url_domain(path: str) -> str:
    """Extract the primary feature domain from a URL path.

    Examples:
        /records           → records
        /records/${id}     → records
        /workflows/${p}/s  → workflows
        /api/v1/records    → records  (strips API prefix)
    """
    clean = re.sub(r"\$\{[^}]+\}", ":param", path)
    # Strip common API prefixes
    for prefix in ("/api/v1/", "/api/v2/", "/api/", "/v1/", "/v2/"):
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):]
            break
    segments = [s for s in clean.strip("/").split("/") if s and s != ":param" and not s.startswith(":")]
    return segments[0].lower() if segments else ""


def _url_segments(path: str) -> set[str]:
    """Extract ALL meaningful path segments from a URL, lowercased.

    Examples:
        /api/catalogue/products     → {"api", "catalogue", "products"}
        /api/commande/cart/items    → {"api", "commande", "cart", "items"}
        /api/search/search?q=foo    → {"api", "search"}
    """
    clean = re.sub(r"\$\{[^}]+\}", "", path)
    clean = re.sub(r"\{[^}]+\}", "", clean)  # Java path params {id}
    clean = clean.split("?")[0]  # Strip query params
    segments = {s.lower() for s in clean.strip("/").split("/")
                if s and not s.startswith(":")}
    segments.discard("api")
    segments.discard("v1")
    segments.discard("v2")
    return segments


def _infer_domain_from_filename(path: str) -> str:
    """Extract the feature domain from a frontend filename.

    Examples:
        useRecords.ts                → records
        workflow-api.service.ts      → workflow
        useWorkflowVariants.ts       → workflow-variants
        notification-api.service.ts  → notification
    """
    name = PurePosixPath(path).name
    # Strip extensions iteratively
    for _ in range(3):
        prev = name
        name = re.sub(r"\.(tsx?|jsx?|service|api)$", "", name, flags=re.IGNORECASE)
        if name == prev:
            break
    # Remove common prefixes
    name = re.sub(r"^use", "", name, flags=re.IGNORECASE)
    # Remove common suffixes
    name = re.sub(
        r"[-.]?(api|service|hook|page|component|container|context|provider)$",
        "", name, flags=re.IGNORECASE,
    )
    # camelCase → kebab-case
    name = re.sub(r"([a-z])([A-Z])", r"\1-\2", name).lower()
    name = name.strip("-_.").replace("_", "-")
    return name or ""


def _feature_names_match(backend_name: str, frontend_domain: str) -> bool:
    """Check if a backend feature name likely corresponds to a frontend domain.

    Uses normalized containment matching to handle plurals and compound names.
    """
    a = backend_name.lower().replace("-", "").replace("_", "")
    b = frontend_domain.lower().replace("-", "").replace("_", "")
    if not a or not b or len(a) < 3 or len(b) < 3:
        return a == b
    return a == b or a.startswith(b) or b.startswith(a) or a in b or b in a


def _normalize_url(path: str) -> str:
    """Normalize a URL path for comparison."""
    normalized = re.sub(r"\$\{[^}]+\}", ":param", path)
    normalized = re.sub(r":\w+", ":param", normalized)
    normalized = normalized.rstrip("/")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized.lower()


def _should_exclude_frontend(path: str, content: str) -> bool:
    """Check if a frontend file should be excluded from slices."""
    lower = path.lower()
    # Keep static assets for frameworks (e.g., Odoo) where frontend lives under static/
    if "/static/" in lower:
        return False
    # Build / tooling dirs
    if any(seg in lower for seg in (
        "node_modules", "/dist/", "/build/", "/.git/", "/coverage/",
        "/.next/", "/.turbo/", "/storybook/", "/__mocks__/",
    )):
        return True
    # Test / spec / mock files
    if any(tok in lower for tok in (
        "test.", "spec.", "__tests__", ".setup.", ".mock.", ".stories.",
    )):
        return True
    # Config files
    if any(tok in lower for tok in (
        ".config.", "postcss.", "tailwind.", "vite.config", "eslint.",
        "tsconfig", "prettier", "babel.", "jest.", "vitest.",
    )):
        return True
    # Type declarations
    if lower.endswith(".d.ts"):
        return True
    # Styling / assets
    if any(lower.endswith(ext) for ext in (
        ".css", ".scss", ".less", ".svg", ".png", ".jpg", ".ico", ".json",
    )):
        return True
    # Large locale/translation files
    if "/locales/" in lower or "/i18n/" in lower:
        return True
    # Index re-exports (no real logic, just barrel files)
    if lower.endswith("/index.ts") and "export *" in content:
        return True
    # Main entry / root app file (too broad, pulls in everything)
    name = PurePosixPath(path).name.lower()
    if name in ("main.ts", "main.tsx", "app.tsx", "app.ts", "index.tsx"):
        return True
    return False


def _top_level_dir(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return parts[0].lower() if parts else ""


def _detect_module_names(paths: set[str]) -> set[str]:
    """Detect module folder names from manifest files (e.g., Odoo __manifest__.py)."""
    modules: set[str] = set()
    for p in paths:
        pp = PurePosixPath(p)
        if pp.name.lower() == "__manifest__.py":
            parent = pp.parent.name.lower()
            if parent:
                modules.add(parent)
    return modules


def _module_name_for_path(path: str, module_names: set[str]) -> str:
    """Return the first matching module folder in a path."""
    if not module_names:
        return ""
    for seg in path.lower().split("/"):
        if seg in module_names:
            return seg
    return ""


def _build_frontend_slice(
    entry_files: set[str],
    imports_by_file: dict[str, list[str]],
    all_files: dict[str, str],
) -> dict[str, str]:
    """Build a frontend slice by tracing imports from matched entry files."""
    queue = sorted(entry_files)
    seen: set[str] = set()
    included: list[str] = []
    max_files = 15

    while queue and len(included) < max_files:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if current not in all_files:
            continue

        content = all_files[current]
        if _should_exclude_frontend(current, content) and current not in entry_files:
            continue

        included.append(current)
        for dep in sorted(imports_by_file.get(current, [])):
            if dep not in seen:
                queue.append(dep)

    return {path: all_files[path] for path in included if path in all_files}


# ── Graph data builder ────────────────────────────────────────

_SLICE_COLORS = [
    "#3498db", "#2ecc71", "#e74c3c", "#f39c12", "#9b59b6",
    "#1abc9c", "#e67e22", "#34495e", "#16a085", "#c0392b",
    "#2980b9", "#27ae60", "#8e44ad", "#d35400", "#2c3e50",
]


def _file_type(path: str, side: str) -> str:
    """Infer a short type label from a file path."""
    lower = path.lower()
    if side == "frontend":
        if "/hooks/" in lower or lower.startswith("use"):
            return "hook"
        if "/pages/" in lower:
            return "page"
        if "/services/" in lower or ".service." in lower:
            return "service"
        if "/components/" in lower:
            return "component"
        return "frontend"
    # backend
    if "/routes/" in lower or ".routes." in lower:
        return "route"
    if "/controller/" in lower or "controller" in lower:
        return "controller"
    if "/service" in lower:
        return "service"
    if "/model" in lower or "/dto" in lower or "/entity" in lower:
        return "model"
    return "backend"


def _short_label(path: str) -> str:
    return PurePosixPath(path).name


def _build_graph_data(
    pairs: list[FeaturePair],
    be_imports: dict[str, list[str]],
    fe_imports: dict[str, list[str]],
) -> GraphData:
    """Build a serializable graph from pairs and import maps."""
    nodes_map: dict[str, dict] = {}
    edges: list[dict] = []
    slices: list[dict] = []
    file_to_slices: dict[str, list[str]] = {}

    for idx, pair in enumerate(pairs):
        color = _SLICE_COLORS[idx % len(_SLICE_COLORS)]
        slice_node_ids: list[str] = []

        # Backend files
        for fp in pair.backend.files:
            if fp not in nodes_map:
                nodes_map[fp] = {
                    "id": fp, "label": _short_label(fp),
                    "type": _file_type(fp, "backend"), "side": "backend",
                    "slices": [],
                }
            nodes_map[fp]["slices"].append(pair.feature_name)
            slice_node_ids.append(fp)
            file_to_slices.setdefault(fp, []).append(pair.feature_name)

        # Frontend files
        if pair.frontend:
            for fp in pair.frontend.files:
                if fp not in nodes_map:
                    nodes_map[fp] = {
                        "id": fp, "label": _short_label(fp),
                        "type": _file_type(fp, "frontend"), "side": "frontend",
                        "slices": [],
                    }
                nodes_map[fp]["slices"].append(pair.feature_name)
                slice_node_ids.append(fp)
                file_to_slices.setdefault(fp, []).append(pair.feature_name)

        slices.append({
            "name": pair.feature_name,
            "entrypoint": pair.backend.entrypoint,
            "node_ids": slice_node_ids,
            "color": color,
            "has_frontend": pair.frontend is not None,
        })

    # Edges from import maps (backend)
    seen_edges: set[tuple[str, str]] = set()
    for source, targets in be_imports.items():
        if source not in nodes_map:
            continue
        for target in targets:
            if target in nodes_map and (source, target) not in seen_edges:
                seen_edges.add((source, target))
                edges.append({"source": source, "target": target, "type": "import"})

    # Edges from import maps (frontend)
    for source, targets in fe_imports.items():
        if source not in nodes_map:
            continue
        for target in targets:
            if target in nodes_map and (source, target) not in seen_edges:
                seen_edges.add((source, target))
                edges.append({"source": source, "target": target, "type": "import"})

    # Pairing edges: connect frontend entry files to backend entry files within each slice
    for pair in pairs:
        if not pair.frontend or not pair.frontend.files:
            continue
        backend_entry = pair.backend.entrypoint
        if backend_entry not in nodes_map:
            continue
        # Connect each frontend file to the backend entry (pairing relationship)
        for fe_file in pair.frontend.files:
            if fe_file in nodes_map and (fe_file, backend_entry) not in seen_edges:
                seen_edges.add((fe_file, backend_entry))
                edges.append({
                    "source": fe_file,
                    "target": backend_entry,
                    "type": "pairing",
                    "feature": pair.feature_name,
                })

    return GraphData(
        nodes=list(nodes_map.values()),
        edges=edges,
        slices=slices,
    )


# ── Pairing engine ───────────────────────────────────────────

def build_feature_pairs(
    backend_files: dict[str, str],
    frontend_files: dict[str, str],
    on_log: Optional[callable] = None,
) -> tuple[list[FeaturePair], GraphData]:
    """Build paired backend + frontend feature slices.

    1. Slices backend into feature chunks (routes → deps)
    2. Indexes frontend files by the API URL domains they reference
    3. Matches each backend slice to frontend files via domain overlap
    4. Traces frontend imports from matched hooks/services
    5. Returns (FeaturePairs, GraphData) tuple
    """
    def _log(msg: str) -> None:
        print(msg)
        if on_log:
            on_log(msg)

    # Sort the incoming file dictionaries to guarantee deterministic iteration order
    # (The frontend uses Promise.all(), so the JSON keys arrive in non-deterministic order)
    backend_files = {k: backend_files[k] for k in sorted(backend_files.keys())}
    frontend_files = {k: frontend_files[k] for k in sorted(frontend_files.keys())}

    # ── Step 1: backend slicing ──────────────────────────────
    backend_slices = build_dependency_slices(backend_files)
    if not backend_slices:
        _log("[slicer] No backend slices found — check that route/controller files are present")
        return [], GraphData()

    _log(f"[slicer] Found {len(backend_slices)} backend slice(s)")

    # Build backend import graph for graph visualization
    be_normalized = {
        _norm_path(p): backend_files[p]
        for p in sorted(backend_files.keys())
        if not _should_exclude(_norm_path(p), backend_files[p])
    }
    be_path_index = set(be_normalized.keys())
    be_imports: dict[str, list[str]] = {
        p: _extract_imports(p, be_normalized[p], be_path_index)
        for p in sorted(be_normalized.keys())
    }

    # ── Step 2: frontend analysis ────────────────────────────
    # Pre-filter: drop obviously irrelevant frontend files before indexing
    fe_normalized = {
        _norm_path(p): frontend_files[p]
        for p in sorted(frontend_files.keys())
        if not _should_exclude_frontend(_norm_path(p), frontend_files[p])
    }
    fe_path_index = set(fe_normalized.keys())
    fe_imports: dict[str, list[str]] = {
        p: _extract_imports(p, fe_normalized[p], fe_path_index)
        for p in sorted(fe_normalized.keys())
    }
    detected_modules = _detect_module_names(set(be_normalized.keys()) | set(fe_normalized.keys()))
    all_fe_top_roots = {_top_level_dir(p) for p in fe_normalized if _top_level_dir(p)}

    # Build domain → frontend files index from two signals:
    # (a) URL domains extracted from apiClient / fetch calls
    # (b) Feature domain inferred from filenames
    domain_to_fe: dict[str, set[str]] = {}

    # Also build a per-file index of all URL segments for fuzzy matching
    fe_url_segments: dict[str, set[str]] = {}

    for fe_path in sorted(fe_normalized.keys()):
        fe_content = fe_normalized[fe_path]
        # Signal A: API URL domains
        file_all_segments: set[str] = set()
        for url in _extract_api_urls(fe_content):
            domain = _url_domain(url)
            if domain:
                domain_to_fe.setdefault(domain, set()).add(fe_path)
            # Also collect all URL segments for fuzzy matching
            file_all_segments.update(_url_segments(url))

        if file_all_segments:
            fe_url_segments[fe_path] = file_all_segments

        # Signal B: Filename domain (for likely entry files)
        lower = fe_path.lower()
        name_lower = PurePosixPath(fe_path).name.lower()
        is_hook = "/hooks/" in lower or name_lower.startswith("use")
        is_service = name_lower.endswith(".service.ts") or "/services/" in lower
        is_page = "/pages/" in lower
        # Angular: components, modules, resolvers, guards
        is_component = name_lower.endswith(".component.ts") or "/components/" in lower
        is_module = name_lower.endswith(".module.ts")
        is_resolver = name_lower.endswith(".resolver.ts") or "/resolvers/" in lower
        if is_hook or is_service or is_page or is_component or is_module or is_resolver:
            file_domain = _infer_domain_from_filename(fe_path)
            if file_domain:
                domain_to_fe.setdefault(file_domain, set()).add(fe_path)

    fe_with_urls = len(fe_url_segments)
    fe_with_domains = sum(1 for fes in domain_to_fe.values() for _ in fes)
    _log(
        f"[slicer] Frontend index: {len(fe_normalized)} files after filtering, "
        f"{fe_with_urls} with API URLs, {len(domain_to_fe)} domain(s) detected"
    )
    if domain_to_fe:
        for dom, fes in sorted(domain_to_fe.items()):
            _log(f"[slicer]   domain '{dom}': {len(fes)} file(s)")

    # ── Step 3: match and pair ───────────────────────────────
    pairs: list[FeaturePair] = []

    for be_slice in backend_slices:
        matched_fe_files: set[str] = set()

        # Strategy 1: Match by feature domain (exact / substring)
        for domain, fe_files in sorted(domain_to_fe.items()):
            if _feature_names_match(be_slice.feature_name, domain):
                matched_fe_files.update(fe_files)

        # Strategy 2: Match by URL segment overlap
        # Collect segments from all backend endpoints in this slice
        be_segments: set[str] = set()
        for ep in be_slice.endpoints:
            be_segments.update(_url_segments(ep.path))
        # Also add the feature name itself and its parts
        be_feature_parts = set(be_slice.feature_name.lower().replace("_", "-").split("-"))
        be_segments.update(be_feature_parts)
        # Also add segments from the directory path (e.g. "catalogue" from "catalogue-service/...")
        for be_path in be_slice.files:
            for seg in be_path.lower().replace("_", "-").split("/"):
                if seg and len(seg) > 2 and seg not in ("src", "main", "java", "com", "controller", "model", "service"):
                    be_segments.add(seg.replace("-service", ""))

        if be_segments:
            for fe_path in sorted(fe_url_segments.keys()):
                fe_segs = fe_url_segments[fe_path]
                # Check if any backend segment overlaps with frontend URL segments
                overlap = be_segments & fe_segs
                if overlap:
                    matched_fe_files.add(fe_path)

        # Strategy 3: Module-path fallback for mixed monorepo/module frameworks
        # (e.g., Odoo modules where backend/frontend are colocated by folder).
        if not matched_fe_files:
            be_modules = {
                _module_name_for_path(p, detected_modules)
                for p in be_slice.files
                if _module_name_for_path(p, detected_modules)
            }
            if be_modules:
                for fe_path in sorted(fe_normalized.keys()):
                    fe_module = _module_name_for_path(fe_path, detected_modules)
                    fe_lower = fe_path.lower()
                    # Must be in the SAME module AND in a frontend-like subfolder
                    if fe_module in be_modules and (
                        f"/{fe_module}/views/" in fe_lower
                        or f"/{fe_module}/wizards/" in fe_lower
                        or f"/{fe_module}/static/" in fe_lower
                    ):
                        matched_fe_files.add(fe_path)
                # Hard cap: prevent one slice from absorbing the entire frontend
                if len(matched_fe_files) > 20:
                    _log(
                        f"[slicer] Feature '{be_slice.feature_name}': "
                        f"module fallback capped from {len(matched_fe_files)} to 20 files"
                    )
                    matched_fe_files = set(sorted(matched_fe_files)[:20])
                if matched_fe_files:
                    _log(
                        f"[slicer] Feature '{be_slice.feature_name}': "
                        f"module-name fallback matched {len(matched_fe_files)} frontend file(s)"
                    )
            else:
                # Last resort: top-level folder match, but only if roots are not globally collapsed.
                be_roots = {_top_level_dir(p) for p in be_slice.files if _top_level_dir(p)}
                if be_roots and len(all_fe_top_roots) > 1:
                    for fe_path in sorted(fe_normalized.keys()):
                        fe_root = _top_level_dir(fe_path)
                        fe_lower = fe_path.lower()
                        if fe_root in be_roots and (
                            "/views/" in fe_lower
                            or "/wizards/" in fe_lower
                            or "/static/" in fe_lower
                        ):
                            matched_fe_files.add(fe_path)
                    # Hard cap on top-level fallback too
                    if len(matched_fe_files) > 20:
                        matched_fe_files = set(sorted(matched_fe_files)[:20])
                    if matched_fe_files:
                        _log(
                            f"[slicer] Feature '{be_slice.feature_name}': "
                            f"top-level fallback matched {len(matched_fe_files)} frontend file(s)"
                        )

        # Log pairing results
        if matched_fe_files:
            _log(f"[slicer] Feature '{be_slice.feature_name}': matched {len(matched_fe_files)} frontend file(s)")
        else:
            _log(f"[slicer] Feature '{be_slice.feature_name}': no frontend matches (backend-only)")

        # Build frontend slice
        frontend_slice: Optional[FrontendSlice] = None
        if matched_fe_files:
            fe_slice_files = _build_frontend_slice(matched_fe_files, fe_imports, fe_normalized)
            if fe_slice_files:
                # Collect all API URLs from the matched entry files
                matched_urls: list[str] = []
                for fp in sorted(matched_fe_files):
                    if fp in fe_normalized:
                        matched_urls.extend(_extract_api_urls(fe_normalized[fp]))

                all_fe_text = "\n".join(fe_slice_files.values())
                frontend_slice = FrontendSlice(
                    feature_name=be_slice.feature_name,
                    files=fe_slice_files,
                    entry_hooks=sorted(matched_fe_files),
                    api_urls=_dedupe_preserve_order(matched_urls),
                    estimated_tokens=estimate_tokens(all_fe_text),
                )

        # Compute combined token estimate
        be_tokens = be_slice.estimated_tokens
        fe_tokens = frontend_slice.estimated_tokens if frontend_slice else 0

        if be_slice.feature_name == "index":
            fe_paths = sorted(frontend_slice.files.keys()) if frontend_slice else []
            fe_hooks = frontend_slice.entry_hooks if frontend_slice else []
            be_paths = sorted(be_slice.files.keys())
            _log(f"[slicer-debug] index feature exact paths:")
            _log(f"  BE files: {be_paths}")
            _log(f"  FE files: {fe_paths}")
            _log(f"  FE hooks: {fe_hooks}")

        pairs.append(FeaturePair(
            feature_name=be_slice.feature_name,
            backend=be_slice,
            frontend=frontend_slice,
            estimated_tokens=be_tokens + fe_tokens,
        ))

    # ── Step 4: build graph data ─────────────────────────────
    graph = _build_graph_data(pairs, be_imports, fe_imports)

    return pairs, graph
