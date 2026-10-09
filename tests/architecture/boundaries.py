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
- ``check_runtime_neutral`` imports the runtime-neutral modules (shared by
  every execution runtime) in a fresh interpreter and reports any Temporal
  module they load; ``check_sources`` rejects their direct Temporal imports.
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

# Runtime-neutral modules (dotted paths below the package): shared by every
# execution runtime, so they never depend on Temporal - neither the SDK, nor
# Pydantic AI's Temporal integration, nor the project's Temporal adapter -
# directly (``check_sources``) or transitively (``check_runtime_neutral``).
# Pydantic AI's engine-neutral ``pydantic_ai.durable_exec`` package is imported
# by Pydantic AI itself and is not Temporal.
RUNTIME_NEUTRAL_MODULES = (
    "adapters.agent",
    "adapters.models",
    "adapters.local",
    "bootstrap.investigations",
    "bootstrap.local_investigations",
)
TEMPORAL_MODULES = ("temporalio", "pydantic_ai.durable_exec.temporal")
TEMPORAL_ADAPTER = "adapters.temporal"


def _within(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def is_runtime_neutral(module: str, package: str) -> bool:
    return any(_within(module, f"{package}.{m}") for m in RUNTIME_NEUTRAL_MODULES)


def is_temporal(module: str, package: str) -> bool:
    return any(
        _within(module, m) for m in (*TEMPORAL_MODULES, f"{package}.{TEMPORAL_ADAPTER}")
    )


# Narrow, justified exceptions: {(module, subject): reason}. The subject is the
# imported module for import rules and the class name for layout rules.
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
        neutral = is_runtime_neutral(module, package)
        for target, line in _imported_names(tree, module, is_package):
            if (module, target) in EXCEPTIONS:
                continue
            if neutral and is_temporal(target, package):
                violations.add(
                    Violation(
                        module, line, f"runtime-neutral code must not import {target}"
                    )
                )
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


# --- Application layout: ports vs contracts vs services -----------------------
#
# ``application/ports/*``     Protocol interfaces only.
# ``application/contracts/*`` shared data types only (``contracts/__init__`` also
#                             holds the versioned wire-contract base).
# ``application/<area>.py``   services/use cases; never define a Protocol.
#
# An exception to any of these rules goes in ``EXCEPTIONS`` with a reason, keyed
# by (module, subject): the class name for rules on definitions, the imported
# module for rules on imports.

WIRE_CONTRACT_BASE_NAMES = frozenset(
    {"CONTRACT_VERSION", "ContractModel", "Identifier"}
)
SERVICE_CLASS_SUFFIXES = (
    "Service",
    "Gateway",
    "Runner",
    "Registry",
    "Handler",
    "Manager",
    "Orchestrator",
    "Repository",
    "Store",
)
_TYPE_ALIAS_CALLS = frozenset(
    {"TypeVar", "ParamSpec", "TypeVarTuple", "NewType", "TypeAliasType"}
)


def _application_area(module: str, package: str) -> str | None:
    """'ports', 'contracts' or 'service' for modules under ``application``."""
    parts = module.split(".")
    if len(parts) < 3 or parts[0] != package or parts[1] != "application":
        return None
    return parts[2] if parts[2] in ("ports", "contracts") else "service"


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_protocol_class(node: ast.ClassDef) -> bool:
    return any(_base_name(base) == "Protocol" for base in node.bases)


def _trivial_body(body: list[ast.stmt]) -> bool:
    """True for a docstring/``...``/``pass`` body: an interface, not logic."""
    return all(
        isinstance(stmt, ast.Pass)
        or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        for stmt in body
    )


def _is_type_expression(node: ast.expr | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return node.value is None or isinstance(node.value, str)
    if isinstance(node, ast.Name | ast.Attribute):
        return True
    if isinstance(node, ast.Subscript):
        return _is_type_expression(node.value)
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.BitOr)
    if isinstance(node, ast.Call):
        return _base_name(node.func) in _TYPE_ALIAS_CALLS
    return False


def _port_violations(
    body: list[ast.stmt], protocols: set[str]
) -> list[tuple[int, str, str]]:
    """(line, subject, detail) for everything in a ports module that is not a port."""
    found: list[tuple[int, str, str]] = []
    for node in body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        if isinstance(node, ast.If) and _base_name(node.test) == "TYPE_CHECKING":
            found += _port_violations(node.body, protocols)
            found += _port_violations(node.orelse, protocols)
        elif isinstance(node, ast.ClassDef):
            inherits_ports = bool(node.bases) and all(
                _base_name(base) in protocols for base in node.bases
            )
            if not (_is_protocol_class(node) or inherits_ports):
                found.append(
                    (node.lineno, node.name, "ports may only define Protocols")
                )
                continue
            protocols.add(node.name)
            for member in node.body:
                if isinstance(
                    member, ast.FunctionDef | ast.AsyncFunctionDef
                ) and not _trivial_body(member.body):
                    found.append(
                        (
                            member.lineno,
                            f"{node.name}.{member.name}",
                            "port methods must be bodiless (docstring, ... or pass)",
                        )
                    )
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.TypeAlias):
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if names == ["__all__"]:
                    continue
            value = None if isinstance(node, ast.TypeAlias) else node.value
            if not _is_type_expression(value):
                found.append(
                    (node.lineno, "assignment", "ports hold no values or data")
                )
        else:
            found.append(
                (
                    node.lineno,
                    type(node).__name__,
                    "ports may only contain Protocols, imports and type aliases",
                )
            )
    return found


def _service_like_reason(node: ast.ClassDef) -> str | None:
    if node.name.endswith(SERVICE_CLASS_SUFFIXES):
        return "service-like class name"
    for member in node.body:
        if isinstance(member, ast.AsyncFunctionDef):
            return f"async method {member.name}"
        if isinstance(member, ast.FunctionDef) and member.name == "__init__":
            return "hand-written __init__ (injected collaborators)"
    return None


def _application_import_violation(area: str, target: str, package: str) -> str | None:
    """Reason an import in a port/contract module is forbidden, or None."""
    app = f"{package}.application"
    if target == app or target.startswith(f"{app}.contracts"):
        return None
    if target.startswith(f"{app}.ports"):
        return "contracts must not import ports" if area == "contracts" else None
    if target.startswith(f"{app}."):
        return f"{area} must not import the service module {target}"
    layer = layer_of(target, package)
    if layer in ("adapters", "interfaces", "bootstrap", "capabilities"):
        return f"{area} must not import {layer} ({target})"
    return None


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def check_application_layout(package_dir: Path, package: str) -> list[Violation]:
    """Check where Protocols, data types and services live under ``application``.

    Rules: Protocols only in ``application/ports``; ports contain only Protocols;
    contracts contain no Protocols or services and keep the wire-contract base;
    ports/contracts never import service modules, adapters, interfaces or
    bootstrap; adapters take port interfaces from ``application.ports``.
    """
    violations: set[Violation] = set()
    trees: dict[str, tuple[ast.Module, bool]] = {}
    for module, path in iter_modules(package_dir, package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        trees[module] = (tree, path.name == "__init__.py")

    def report(module: str, line: int, subject: str, detail: str) -> None:
        if (module, subject) not in EXCEPTIONS:
            violations.add(Violation(module, line, detail))

    port_names: set[str] = set()
    for module, (tree, _) in trees.items():
        if _application_area(module, package) == "ports":
            for line, subject, detail in _port_violations(tree.body, port_names):
                report(module, line, subject, f"{detail} ({subject})")

    for module, (tree, is_package) in trees.items():
        area = _application_area(module, package)
        if area is not None and area != "ports":
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and _is_protocol_class(node):
                    report(
                        module,
                        node.lineno,
                        node.name,
                        f"Protocol {node.name} must live in application/ports",
                    )
        if area == "contracts":
            errors: set[str] = set()  # error types may carry data via __init__
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                if any(
                    _base_name(b).endswith(("Error", "Exception"))
                    or _base_name(b) in errors
                    for b in node.bases
                ):
                    errors.add(node.name)
                    continue
                reason = _service_like_reason(node)
                if reason:
                    report(
                        module,
                        node.lineno,
                        node.name,
                        f"contract {node.name} looks like a service: {reason}",
                    )
        if module == f"{package}.application.contracts":
            for name in sorted(WIRE_CONTRACT_BASE_NAMES - _top_level_names(tree)):
                report(module, 1, name, f"contracts/__init__ must define {name}")
        if area in ("ports", "contracts"):
            for target, line in _imported_names(tree, module, is_package):
                reason = _application_import_violation(area, target, package)
                if reason:
                    report(module, line, target, reason)
        if layer_of(module, package) == "adapters":
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                single = ast.Module(body=[node], type_ignores=[])
                for target, line in _imported_names(single, module, is_package):
                    source, _, name = target.rpartition(".")
                    if name in port_names and _application_area(source, package) == (
                        "service"
                    ):
                        report(
                            module,
                            line,
                            target,
                            f"adapters must import port {name} from application.ports,"
                            f" not {source}",
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
print(json.dumps({"loaded": loaded, "modules": sorted(sys.modules),
                  "effects": effects, "errors": errors}))
"""


def _probe(
    package_root: Path, modules: list[str], allowed: frozenset[str] | None
) -> dict[str, list[str]] | str:
    """Import ``modules`` in a fresh interpreter; the probe report or an error."""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _IMPORT_PROBE,
            str(package_root),
            json.dumps(modules),
            json.dumps(sorted(allowed or ())),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        return f"import probe failed: {result.stderr.strip()[-2000:]}"
    report: dict[str, list[str]] = json.loads(result.stdout.strip().splitlines()[-1])
    return report


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
    report = _probe(package_root, modules, ALLOWED_THIRD_PARTY[layer])
    if isinstance(report, str):
        return [report]
    forbidden = FORBIDDEN_AT_IMPORT_TIME_BY_LAYER[layer]
    problems = [f"import error: {error}" for error in report["errors"]]
    problems += [
        f"{layer} import loads {name}" for name in report["loaded"] if name in forbidden
    ]
    problems += [
        f"{layer} import side effect: {effect}" for effect in report["effects"]
    ]
    return problems


def check_runtime_neutral(package_root: Path, package: str) -> list[str]:
    """Import every runtime-neutral module in a fresh interpreter; report any
    Temporal module that ended up loaded (directly or transitively)."""
    modules = [
        name
        for name, _ in iter_modules(package_root / package, package)
        if is_runtime_neutral(name, package)
    ]
    report = _probe(package_root, modules, None)
    if isinstance(report, str):
        return [report]
    problems = [f"import error: {error}" for error in report["errors"]]
    loaded = [m for m in report["modules"] if is_temporal(m, package)]
    roots = sorted(
        {
            next(
                m
                for m in (*TEMPORAL_MODULES, f"{package}.{TEMPORAL_ADAPTER}")
                if _within(name, m)
            )
            for name in loaded
        }
    )
    problems += [f"runtime-neutral import loads {root}" for root in roots]
    return problems
