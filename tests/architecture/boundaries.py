"""Layer rules and the checkers that enforce them.

This module is the single place that declares which layers and third-party
packages each layer may import. Change a rule here, with a justification, rather
than weakening a test. Two complementary checks use these rules:

- ``check_sources`` parses every module's AST (including imports nested in
  functions and ``TYPE_CHECKING`` blocks) without importing it.
- ``check_import_time`` imports the inner-layer modules in a fresh interpreter
  and reports forbidden packages that ended up loaded (directly or
  transitively) and side effects at import: network, subprocess, file opens
  and environment reads.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LAYERS = (
    "domain",
    "application",
    "capabilities",
    "adapters",
    "interfaces",
    "bootstrap",
)

# Internal layers each layer may import. The package root (version only) is
# importable from everywhere. Bootstrap is the composition root and may import
# anything.
ALLOWED_LAYER_IMPORTS: dict[str, frozenset[str]] = {
    "domain": frozenset({"domain"}),
    "application": frozenset({"domain", "application"}),
    "capabilities": frozenset({"domain", "application", "capabilities"}),
    "adapters": frozenset({"domain", "application", "capabilities", "adapters"}),
    "interfaces": frozenset({"domain", "application", "capabilities", "interfaces"}),
    "bootstrap": frozenset(LAYERS),
}

# Third-party top-level packages each layer may import (allowlist, fail closed).
# None means unrestricted. Stdlib is always allowed except STDLIB_DENIED.
# Justification: pydantic is allowed inward of adapters for typed DTO validation
# at boundaries (application/capabilities) but never in the domain.
_PYDANTIC = frozenset({"pydantic", "annotated_types", "typing_extensions"})
ALLOWED_THIRD_PARTY: dict[str, frozenset[str] | None] = {
    "domain": frozenset(),
    "application": _PYDANTIC,
    "capabilities": _PYDANTIC,
    "adapters": None,
    "interfaces": _PYDANTIC | frozenset({"fastapi", "starlette", "click", "httpx"}),
    "bootstrap": None,
}

# Stdlib modules that would let inner layers bypass the import rules or do
# process-level effects.
INNER_LAYERS = ("domain", "application", "capabilities")
STDLIB_DENIED: dict[str, frozenset[str]] = {
    layer: frozenset({"importlib", "subprocess", "socket"}) for layer in INNER_LAYERS
}

# Packages that must never be loaded by importing an inner layer, even
# transitively. Checked against sys.modules in a fresh interpreter.
FORBIDDEN_AT_IMPORT_TIME: frozenset[str] = frozenset(
    {
        "alembic",
        "click",
        "fastapi",
        "google",
        "grpc",
        "httpx",
        "logfire",
        "mlflow",
        "openai",
        "opentelemetry",
        "prometheus_client",
        "psycopg",
        "pydantic_ai",
        "sqlalchemy",
        "sqlglot",
        "starlette",
        "temporalio",
        "uvicorn",
    }
)
FORBIDDEN_AT_IMPORT_TIME_BY_LAYER: dict[str, frozenset[str]] = {
    "domain": FORBIDDEN_AT_IMPORT_TIME | _PYDANTIC | frozenset({"pydantic_core"}),
    "application": FORBIDDEN_AT_IMPORT_TIME,
    "capabilities": FORBIDDEN_AT_IMPORT_TIME,
}

# Narrow, justified exceptions: {(importing module, imported module): reason}.
EXCEPTIONS: dict[tuple[str, str], str] = {}


@dataclass(frozen=True, order=True)
class Violation:
    module: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line}: {self.detail}"


def iter_modules(package_dir: Path, package: str) -> list[tuple[str, Path]]:
    """All modules under ``package_dir`` as (dotted name, path)."""
    modules = []
    for path in sorted(package_dir.rglob("*.py")):
        parts = path.relative_to(package_dir).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append((".".join((package, *parts)), path))
    return modules


def layer_of(module: str, package: str) -> str | None:
    parts = module.split(".")
    if parts[0] != package or len(parts) < 2:
        return None
    return parts[1] if parts[1] in LAYERS else None


def _imported_names(
    tree: ast.AST, module: str, is_package: bool
) -> list[tuple[str, int]]:
    names: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".")
                drop = node.level - 1 if is_package else node.level
                base = base[: len(base) - drop] if drop else base
                target = ".".join([*base, node.module] if node.module else base)
            else:
                target = node.module or ""
            names.append((target, node.lineno))
            # ``from pkg import layer`` imports a submodule, so check those too.
            names.extend(
                (f"{target}.{alias.name}", node.lineno) for alias in node.names
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ):
            names.append(("importlib", node.lineno))
    return names


def check_sources(package_dir: Path, package: str) -> list[Violation]:
    """Statically check every module under ``package_dir`` against the rules."""
    violations: set[Violation] = set()
    stdlib = sys.stdlib_module_names
    for module, path in iter_modules(package_dir, package):
        source_layer = layer_of(module, package)
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for target, line in _imported_names(tree, module, is_package):
            if (module, target) in EXCEPTIONS:
                continue
            top = target.split(".")[0]
            if top == "__future__":
                continue
            if top == package:
                target_layer = layer_of(target, package)
                if target_layer is None:
                    continue  # package root: version metadata only
                if source_layer is None:
                    violations.add(
                        Violation(module, line, f"package root imports layer {target}")
                    )
                elif target_layer not in ALLOWED_LAYER_IMPORTS[source_layer]:
                    violations.add(
                        Violation(
                            module,
                            line,
                            f"{source_layer} must not import {target_layer} ({target})",
                        )
                    )
                continue
            if source_layer is None:
                continue
            if top in stdlib:
                if top in STDLIB_DENIED.get(source_layer, frozenset()):
                    violations.add(
                        Violation(module, line, f"{source_layer} must not use {top}")
                    )
                continue
            allowed = ALLOWED_THIRD_PARTY[source_layer]
            if allowed is not None and top not in allowed:
                violations.add(
                    Violation(module, line, f"{source_layer} must not import {top}")
                )
    return sorted(violations)


_IMPORT_PROBE = r"""
import importlib, json, os, sys, sysconfig

root, modules = sys.argv[1], json.loads(sys.argv[2])
allowed_libraries = json.loads(sys.argv[3])
sys.path.insert(0, root)
effects = []
ALLOWED_SUFFIXES = (".py", ".pyc", ".so", ".pyd", ".pth", ".typed")
STDLIB = tuple(
    {os.path.realpath(sysconfig.get_path(k)) for k in ("stdlib", "platstdlib")}
)
SITE = tuple(
    {os.path.realpath(sysconfig.get_path(k)) for k in ("purelib", "platlib")}
)

# Third-party packages the layer may use are imported before watching, and
# effects they perform themselves (e.g. pydantic reading its own plugin
# settings when a model class is created) are theirs, not the layer's. An
# effect is attributed to the nearest non-stdlib frame that performed it.
library_dirs = []
for name in allowed_libraries:
    try:
        module = importlib.import_module(name)
    except ImportError:
        continue
    library_dirs.extend(getattr(module, "__path__", []))
    if getattr(module, "__file__", None):
        library_dirs.append(os.path.dirname(module.__file__))
library_dirs = tuple(os.path.realpath(d) + os.sep for d in library_dirs)

def performed_by_allowed_library():
    frame = sys._getframe(2)
    while frame is not None:
        filename = frame.f_code.co_filename
        if not filename.startswith("<"):
            path = os.path.realpath(filename)
            if path.startswith(SITE) or not path.startswith(STDLIB):
                return path.startswith(library_dirs)
        frame = frame.f_back
    return False

def record(effect):
    if not performed_by_allowed_library():
        effects.append(effect)

def hook(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.bind",
                 "subprocess.Popen", "os.system", "os.exec", "os.spawn"):
        effects.append(event)
    elif event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0])
        if not path.endswith(ALLOWED_SUFFIXES) and not os.path.isdir(path):
            record("open " + os.path.basename(path))

class WatchedEnviron(dict):
    def __getitem__(self, key):
        record("environ read " + str(key))
        return super().__getitem__(key)
    def get(self, key, default=None):
        record("environ read " + str(key))
        return super().get(key, default)

os.environ = WatchedEnviron(os.environ)
sys.addaudithook(hook)
errors = []
for name in modules:
    try:
        importlib.import_module(name)
    except Exception as exc:
        errors.append(name + ": " + type(exc).__name__)
loaded = sorted({m.split(".")[0] for m in sys.modules})
print(json.dumps({"loaded": loaded, "effects": effects, "errors": errors}))
"""


def check_import_time(package_root: Path, package: str, layer: str) -> list[str]:
    """Import every module of ``layer`` in a fresh interpreter; report problems.

    ``package_root`` is the directory that contains the ``package`` directory.
    """
    modules = [
        name
        for name, _ in iter_modules(
            package_root / package / layer, f"{package}.{layer}"
        )
    ]
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _IMPORT_PROBE,
            str(package_root),
            json.dumps(modules),
            json.dumps(sorted(ALLOWED_THIRD_PARTY[layer] or ())),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        return [f"import probe failed: {result.stderr.strip()[-2000:]}"]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    forbidden = FORBIDDEN_AT_IMPORT_TIME_BY_LAYER[layer]
    problems = [f"import error: {error}" for error in report["errors"]]
    problems += [
        f"{layer} import loads {name}" for name in report["loaded"] if name in forbidden
    ]
    problems += [
        f"{layer} import side effect: {effect}" for effect in report["effects"]
    ]
    return problems
