"""Native contract guard: AST-only source checks and pure runtime comparisons."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).with_name("native_tool_contract_baseline.json")
BUILTIN_REGISTRY = "ToukaProxy/src/tools/builtin/__init__.py"
MEMORY_REGISTRY = "ToukaMemory/src/memory_server/tools/__init__.py"
PROXY_CONTRACTS = "ToukaProxy/src/tools/contracts.py"
MEMORY_CONTRACTS = "ToukaMemory/src/memory_server/tools/contracts.py"


def assigned_node(source: str, name: str) -> ast.AST | None:
    """Return the value of the finite module-level declaration."""
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            return statement.value
    return None


def assigned_keys(source: str, name: str) -> set[str]:
    """Collect literal names without evaluating the module."""
    node = assigned_node(source, name)
    if not isinstance(node, ast.Dict):
        return set()
    return {
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def assigned_string_set(source: str, name: str) -> set[str] | None:
    """Read one literal frozenset({...}) of strings; any other declaration fails closed."""
    declarations = [
        statement.value
        for statement in ast.parse(source).body
        if isinstance(statement, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in statement.targets)
    ]
    if len(declarations) != 1:
        return None
    node = declarations[0]
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Set)
        and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in node.args[0].elts
        )
    ):
        return None
    return {item.value for item in node.args[0].elts}


def registry_bindings(source: str, name: str) -> dict[str, Any]:
    """Bind literal names to handler expressions and their import sources."""
    node = assigned_node(source, name)
    handlers = {}
    if isinstance(node, ast.Dict):
        handlers = {
            key.value: ast.dump(value, include_attributes=False)
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    imports = [
        ast.dump(item, include_attributes=False)
        for item in ast.parse(source).body
        if isinstance(item, (ast.Import, ast.ImportFrom))
    ]
    return {"handlers": handlers, "imports": imports}


def execution_binding_metadata(source: str) -> tuple[dict[str, Any], list[str]]:
    """Read the finite immutable binding table without importing product code."""
    references = [
        statement
        for statement in ast.parse(source).body
        if any(
            isinstance(child, ast.Name) and child.id == "BUILTIN_EXECUTION_BINDINGS"
            for child in ast.walk(statement)
        )
    ]
    if len(references) != 1:
        return {}, ["Execution bindings cannot be reassigned or mutated"]
    node = assigned_node(source, "BUILTIN_EXECUTION_BINDINGS")
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "MappingProxyType"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Dict)
    ):
        return {}, ["Execution bindings require one immutable literal mapping"]
    registry_names = assigned_keys(source, "tool_registry")
    result: dict[str, Any] = {}
    errors = []
    for key, value in zip(node.args[0].keys, node.args[0].values, strict=True):
        if not (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "ExecutionBinding"
            and len(value.args) == 3
            and not value.keywords
        ):
            errors.append("Unsupported execution binding expression")
            continue
        canonical, handler, kind = value.args
        if not (
            isinstance(canonical, ast.Constant)
            and isinstance(canonical.value, str)
            and canonical.value in registry_names
            and isinstance(handler, ast.Subscript)
            and isinstance(handler.value, ast.Name)
            and handler.value.id == "tool_registry"
            and isinstance(handler.slice, ast.Constant)
            and handler.slice.value == key.value
            and key.value in registry_names
            and isinstance(kind, ast.Attribute)
            and isinstance(kind.value, ast.Name)
            and kind.value.id == "ToolExecutionClass"
            and kind.attr in {"TERMINAL", "SELF_APPROVING"}
        ):
            errors.append(f"Invalid execution binding: {key.value}")
            continue
        if key.value in result:
            errors.append(f"Duplicate execution binding: {key.value}")
        result[key.value] = {
            "canonical_name": canonical.value,
            "registry_name": handler.slice.value,
            "kind": kind.attr,
        }
    for name, binding in result.items():
        canonical = result.get(binding["canonical_name"])
        if canonical is None or canonical["kind"] != binding["kind"]:
            errors.append(f"Execution alias does not match canonical classification: {name}")
    return result, errors


def registry_shape_errors(source: str, name: str) -> list[str]:
    """Only one literal registry declaration is supported; mutations fail closed."""
    declarations = 0
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            declarations += 1
            if not isinstance(statement.value, ast.Dict) or any(
                not isinstance(key, ast.Constant) or not isinstance(key.value, str)
                for key in statement.value.keys
            ):
                return [f"Unsupported native registry declaration: {name}"]
        elif any(isinstance(node, ast.Name) and node.id == name for node in ast.walk(statement)):
            if (
                name == "tool_registry"
                and isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and statement.targets[0].id == "BUILTIN_EXECUTION_BINDINGS"
                and not execution_binding_metadata(source)[1]
            ):
                continue  # Exact read-only binding derivation, never a registry mutation.
            return [f"Unsupported native registry reference or mutation: {name}"]
    return [] if declarations == 1 else [f"Native registry declaration count: {name}"]


def selector_actions(source: str, function_name: str) -> set[str]:
    """Finite existing action == literal selectors, without dynamic evaluation."""
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    return {
        comparator.value
        for node in ast.walk(function)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "action"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        for comparator in node.comparators
        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
    }


def consumer_shapes(source: str) -> dict[str, dict[str, Any]]:
    """Record finite argument consumers without evaluating imports or code."""
    result: dict[str, dict[str, Any]] = {}

    def visit(nodes: list[ast.stmt], prefix: str = "") -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parameters = [
                    arg.arg
                    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
                ]
                kwargs = node.args.kwarg.arg if node.args.kwarg else None
                reads: set[tuple[str, str]] = set()
                dynamic: set[str] = set()
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        function = child.func
                        if (
                            isinstance(function, ast.Attribute)
                            and isinstance(function.value, ast.Name)
                            and function.attr in {"get", "pop", "setdefault"}
                            and child.args
                        ):
                            key = child.args[0]
                            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                reads.add((function.value.id, key.value))
                            elif function.value.id == kwargs:
                                dynamic.add(ast.dump(child, include_attributes=False))
                        for keyword in child.keywords:
                            if keyword.arg is None:
                                dynamic.add(ast.dump(keyword.value, include_attributes=False))
                    elif (
                        isinstance(child, ast.Subscript)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == kwargs
                    ):
                        if isinstance(child.slice, ast.Constant) and isinstance(
                            child.slice.value, str
                        ):
                            reads.add((child.value.id, child.slice.value))
                        else:
                            dynamic.add(ast.dump(child, include_attributes=False))
                result[f"{prefix}{node.name}"] = {
                    "parameters": parameters,
                    "kwargs": kwargs,
                    "reads": sorted([list(item) for item in reads]),
                    "dynamic": sorted(dynamic),
                }

    visit(ast.parse(source).body)
    return result


def inventory_errors(registered: set[str], classified: dict[str, str]) -> list[str]:
    """Require one explicit classification for every registered tool."""
    errors = [
        f"Unclassified native tool: {name}" for name in sorted(registered - classified.keys())
    ]
    errors.extend(
        f"Stale native classification: {name}" for name in sorted(classified.keys() - registered)
    )
    errors.extend(
        f"Invalid classification: {name}"
        for name, kind in classified.items()
        if kind not in {"typed", "existing_public", "legacy", "internal"}
    )
    return errors


def consumer_errors(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Identify unclassified changes in finite argument consumers."""
    return [
        f"Unclassified consumer change: {name}"
        for name in sorted(current.keys() | baseline.keys())
        if current.get(name) != baseline.get(name)
    ]


def action_errors(
    handlers: set[str], contracts: set[str], risks: set[str], internal: set[str]
) -> list[str]:
    """Require public actions to match handlers and risk classifications."""
    errors = []
    if handlers != contracts | internal or contracts & internal:
        errors.append("Native action handler/contract/internal classification mismatch")
    if not contracts <= risks:
        errors.append("Public native action lacks source risk classification")
    return errors


def advertisement_errors(names: set[str], legacy: set[str]) -> list[str]:
    """Reject legacy names from model-facing advertisements."""
    return [f"Legacy tool advertised: {name}" for name in sorted(names & legacy)]


def reserved_field_errors(parameters: dict[str, Any], reserved: set[str]) -> list[str]:
    """Keep trusted runtime fields outside public schemas."""
    return [
        f"Runtime field advertised: {name}"
        for name in sorted(set(parameters.get("properties", {})) & reserved)
    ]


def schema_paths(parameters: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Enumerate nested public field paths and array item schemas."""
    paths: dict[str, dict[str, Any]] = {}
    for name, field in parameters.get("properties", {}).items():
        path = f"{prefix}{name}"
        paths[path] = field
        if str(field.get("type", "")).lower() == "array":
            items = field.get("items", {})
            paths[f"{path}[]"] = items
            paths.update(schema_paths(items, f"{path}[]."))
        else:
            paths.update(schema_paths(field, f"{path}."))
    return paths


def schema_errors(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Check portable semantics; provider combinator removal is intentional."""
    wanted, received = schema_paths(expected), schema_paths(actual)
    errors = [f"Schema property drift: {path}" for path in sorted(wanted.keys() ^ received.keys())]
    for path in wanted.keys() & received.keys():
        left, right = wanted[path], received[path]
        for key in ("type", "enum", "description", "required", "maxItems", "minItems"):
            a, b = left.get(key), right.get(key)
            if key == "type":
                a, b = str(a).lower(), str(b).lower()
            if a != b:
                errors.append(f"Schema {key} drift: {path}")
    if set(expected.get("required", [])) != set(actual.get("required", [])):
        errors.append("Schema root required drift")
    return errors


def coverage_errors(expected: set[str], observed: dict[str, Any]) -> list[str]:
    """Find contract paths with no observed backend value."""
    return [
        f"Public field lacks backend observation: {path}"
        for path in sorted(expected - observed.keys())
    ]


def observation_errors(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Compare non-default inputs against actual backend observations."""
    return [
        f"Backend value missing or changed: {path}"
        for path, value in expected.items()
        if path not in actual or actual[path] != value
    ]


def _class_fields(sources: list[str]) -> dict[str, set[str]]:
    classes = {
        node.name: node
        for source in sources
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef)
    }
    result: dict[str, set[str]] = {}

    def fields(name: str, visited: frozenset[str] = frozenset()) -> set[str]:
        if name in result:
            return result[name]
        if name not in classes or name in visited:
            return set()
        node = classes[name]
        value = {
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        }
        for base in node.bases:
            if isinstance(base, ast.Name):
                value |= fields(base.id, visited | {name})
        result[name] = value
        return value

    for name in classes:
        fields(name)
    return result


def run_all(root: Path = PROJECT_ROOT, baseline_path: Path = BASELINE_PATH) -> list[str]:
    """Read only explicit repository source paths; never import the product."""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    sources = {path: (root / path).read_text(encoding="utf-8") for path in baseline["consumers"]}
    for path in (BUILTIN_REGISTRY, MEMORY_REGISTRY, PROXY_CONTRACTS, MEMORY_CONTRACTS):
        sources.setdefault(path, (root / path).read_text(encoding="utf-8"))
    registered = assigned_keys(sources[BUILTIN_REGISTRY], "tool_registry") | assigned_keys(
        sources[MEMORY_REGISTRY], "memory_tool_registry"
    )
    errors = inventory_errors(registered, baseline["tools"])
    for path, registry_name in (
        (BUILTIN_REGISTRY, "tool_registry"),
        (MEMORY_REGISTRY, "memory_tool_registry"),
    ):
        errors.extend(registry_shape_errors(sources[path], registry_name))
        if registry_bindings(sources[path], registry_name) != baseline["registry_bindings"][path]:
            errors.append(f"Native handler/import binding changed: {path}")
    execution_bindings, binding_errors = execution_binding_metadata(sources[BUILTIN_REGISTRY])
    errors.extend(binding_errors)
    if execution_bindings != baseline.get("execution_bindings"):
        errors.append("Trusted execution binding classification changed")
    typed_names = assigned_keys(sources[PROXY_CONTRACTS], "PUBLIC_TOOL_CONTRACTS")
    if typed_names != {name for name, kind in baseline["tools"].items() if kind == "typed"}:
        errors.append("Typed native registration/classification mismatch")
    fields = _class_fields([sources[PROXY_CONTRACTS], sources[MEMORY_CONTRACTS]])
    for path, function_name, contract_name, internal in (
        (
            "ToukaProxy/src/tools/mission_tools.py",
            "mission_tools",
            "MISSION_ACTION_CONTRACTS",
            set(),
        ),
        (
            "ToukaProxy/src/tools/builtin/system.py",
            "update_my_soul",
            "SOUL_ACTION_CONTRACTS",
            {"list", "read"},
        ),
    ):
        declared = assigned_keys(sources[PROXY_CONTRACTS], contract_name)
        if selector_actions(sources[path], function_name) != declared | internal:
            errors.append(f"Unclassified native action selector: {function_name}")
    runtime_fields = _class_fields([sources["ToukaProxy/src/tools/execution_context.py"]])[
        "ToolExecutionContextKwargs"
    ]
    expected_modules = {path for path in baseline["consumers"] if "/tools/" in path} | {
        PROXY_CONTRACTS,
        MEMORY_CONTRACTS,
    }
    actual_modules = {
        path.relative_to(root).as_posix()
        for directory in ("ToukaProxy/src/tools", "ToukaMemory/src/memory_server/tools")
        for path in (root / directory).rglob("*.py")
    }
    if expected_modules != actual_modules:
        errors.append("Native tool source-module inventory changed without classification")
    shapes = {path: consumer_shapes(source) for path, source in sources.items()}
    bindings = {(item["source"], item["function"]): item for item in baseline["typed_consumers"]}
    for path, before in baseline["consumers"].items():
        current = shapes[path]
        for name in current.keys() | before.keys():
            binding = bindings.get((path, name))
            if binding is None:
                errors.extend(
                    f"{path}: {error}"
                    for error in consumer_errors(
                        {name: current.get(name)}, {name: before.get(name)}
                    )
                )
                continue
            if name not in current:
                errors.append(f"Missing typed consumer: {path}:{name}")
                continue
            shape = current[name]
            allowed = set().union(*(fields.get(model, set()) for model in binding["models"]))
            aliases = binding.get("aliases", {})
            ignored = (
                set(binding.get("trusted", [])) | runtime_fields | set(binding.get("internal", {}))
            )
            reads = {
                key for receiver, key in shape["reads"] if receiver in binding.get("receivers", [])
            }
            if binding.get("parameters", True):
                reads |= set(shape["parameters"])
            uncovered = {aliases.get(key, key) for key in reads - ignored} - allowed
            errors.extend(
                f"Undeclared public consumer: {path}:{name}:{key}" for key in sorted(uncovered)
            )
            if shape["dynamic"] != before[name]["dynamic"]:
                errors.append(f"Unclassified dynamic forwarding: {path}:{name}")
    policies = json.loads(
        (root / "ToukaProxy/config/auth_policies.json.example").read_text(encoding="utf-8")
    )
    risks = set(policies["tool_risk"]["tools"]["memory_tools"]["actions"])
    # Internal (model-hidden) actions come from the contract SSOT; tests pin the exact set.
    internal = assigned_string_set(sources[MEMORY_CONTRACTS], "MEMORY_INTERNAL_ACTIONS")
    if internal is None:
        errors.append("Unsupported Memory internal action declaration")
        internal = set()
    errors.extend(
        action_errors(
            assigned_keys(sources[MEMORY_REGISTRY], "MEMORY_ACTIONS"),
            assigned_keys(sources[MEMORY_CONTRACTS], "MEMORY_ACTION_CONTRACTS"),
            risks,
            internal,
        )
    )
    # Legacy entries must never re-enter the Memory-exported schema list.
    schema_source = (root / "ToukaMemory/src/memory_server/tools/schemas.py").read_text(
        encoding="utf-8"
    )
    schema_list = assigned_node(schema_source, "MEMORY_TOOLS_SCHEMA_LIST")
    if not isinstance(schema_list, ast.List) or any(
        not isinstance(item, ast.Name) or item.id != "MEMORY_TOOLS_SCHEMA"
        for item in schema_list.elts
    ):
        errors.append("Unclassified or legacy Memory schema export")
    return errors


def main() -> int:
    """Print source-contract findings and return a process exit code."""
    errors = run_all()
    for error in errors:
        sys.stdout.write(error + "\n")
    sys.stdout.write(
        f"Native tool contracts: {'FAIL' if errors else 'PASS'} ({len(errors)} issues)\n"
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
