"""Standalone smoke sandbox 與計量 API 分類的治理回歸測試。"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import logging
import os
import sys
from pathlib import Path, PureWindowsPath
from types import ModuleType, SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMOKE_DIR = PROJECT_ROOT / "tests" / "smoke"
PRODUCT_IMPORT_PREFIXES = ("src", "memory_server", "ToukaHeartbeat", "awaken")
_PROCESS_ENVIRONMENT = os.environ  # noqa: ENV_SSOT_VIOLATION - sandbox mutation oracle


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _import_name(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return node.names[0].name


def _first_product_import_line(tree: ast.Module) -> int:
    lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and _import_name(node).startswith(PRODUCT_IMPORT_PREFIXES)
    ]
    assert lines, "smoke 腳本應至少載入一個產品模組"
    return min(lines)


def _sandbox_call_lines(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "enable_smoke_sandbox"
    ]


def test_manifest_classifies_every_smoke_script_exactly_once() -> None:
    manifest = _load_module("smoke_manifest", SMOKE_DIR / "manifest.py")
    discovered = {path.name for path in SMOKE_DIR.glob("smoke_*.py")}
    automated = {spec.path for spec in manifest.AUTOMATED_SANDBOXED}
    manual = {spec.path for spec in manifest.MANUAL_LIVE_SPECS}

    assert automated == discovered
    assert not automated & manual
    assert len(automated) == 24
    assert all(
        spec.execution_class == "AUTOMATED_SANDBOXED" for spec in manifest.AUTOMATED_SANDBOXED
    )
    assert all(spec.data_access == "TEMP_SANDBOX" for spec in manifest.AUTOMATED_SANDBOXED)
    assert all(spec.metered_api is False for spec in manifest.AUTOMATED_SANDBOXED)
    assert manifest.MANUAL_LIVE_SPECS
    assert all(spec.metered_api is True for spec in manifest.MANUAL_LIVE_SPECS)


@pytest.mark.parametrize(
    "path",
    sorted(SMOKE_DIR.glob("smoke_*.py")),
    ids=lambda path: path.name,
)
def test_every_automated_smoke_enables_sandbox_before_product_import(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = _sandbox_call_lines(tree)

    assert calls, f"{path.name} 缺 enable_smoke_sandbox()"
    assert min(calls) < _first_product_import_line(tree), (
        f"{path.name} 必須在任何 src/memory_server import 前啟用 sandbox"
    )


def test_sandbox_helper_clears_metered_keys_and_anchors_all_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sandbox = _load_module("smoke_sandbox_env", SMOKE_DIR / "lib" / "sandbox_env.py")
    root = tmp_path / "runner-owned"
    original_env = dict(_PROCESS_ENVIRONMENT)
    try:
        monkeypatch.setenv("TOUKA_SMOKE_SANDBOX_ROOT", str(root))
        monkeypatch.setenv("TOUKA_SMOKE_SANDBOX_OWNER", "runner")
        monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
        monkeypatch.setenv("TAVILY_API_KEY", "must-not-leak")
        monkeypatch.setenv("FOOBAR_API_KEY", "unknown-provider-must-not-leak")
        monkeypatch.setenv("TOUKA_DATA_DIR", str(tmp_path / "unselected-data"))
        monkeypatch.setenv("HEARTBEAT_CONFIG_FILE", str(tmp_path / "unselected-heartbeat.json"))

        result = Path(sandbox.enable_smoke_sandbox("governance"))

        assert result == root.resolve()
        assert Path(_PROCESS_ENVIRONMENT["TOUKA_ENV_FILE"]).parent == result
        assert Path(_PROCESS_ENVIRONMENT["MEMORY_APP_DATA_DIR"]) == result
        assert Path(_PROCESS_ENVIRONMENT["SANCTUARY_DATA_PATH"]).is_relative_to(result)
        assert Path(_PROCESS_ENVIRONMENT["TOUKA_DATA_DIR"]) == result / "data"
        assert Path(_PROCESS_ENVIRONMENT["HEARTBEAT_CONFIG_FILE"]) == result / "heartbeat.json"
        assert Path(_PROCESS_ENVIRONMENT["TOUKA_CONFIG_DIR"]).is_relative_to(result)
        assert Path(_PROCESS_ENVIRONMENT["WORKSPACE_ROOT"]) == result / "workspace"
        assert Path(_PROCESS_ENVIRONMENT["HOST_WORKSPACE_ROOT"]) == result / "workspace"
        assert (result / "workspace").is_dir()
        assert _PROCESS_ENVIRONMENT["TOUKA_BOOTSTRAP_ENABLED"] == "false"
        assert _PROCESS_ENVIRONMENT["DISPATCH_AUTOSPAWN"] == "false"
        assert _PROCESS_ENVIRONMENT["ENABLE_DYNAMIC_MODELS"] == "false"
        assert _PROCESS_ENVIRONMENT["TOUKA_EVAL_JUDGE_ENABLED"] == "false"
        assert _PROCESS_ENVIRONMENT["HF_HUB_OFFLINE"] == "1"
        assert _PROCESS_ENVIRONMENT["TRANSFORMERS_OFFLINE"] == "1"
        assert "OPENAI_API_KEY" not in _PROCESS_ENVIRONMENT
        assert "TAVILY_API_KEY" not in _PROCESS_ENVIRONMENT
        assert "FOOBAR_API_KEY" not in _PROCESS_ENVIRONMENT
        tracked_skill = result / "config" / "skills" / "document-generator" / "SKILL.md"
        source_skill = (
            PROJECT_ROOT / "ToukaProxy" / "config" / "skills" / "document-generator" / "SKILL.md"
        )
        assert tracked_skill.read_bytes() == source_skill.read_bytes()
    finally:
        _PROCESS_ENVIRONMENT.clear()
        _PROCESS_ENVIRONMENT.update(original_env)


def _opts_into_external_network(node: ast.AST) -> bool:
    """enable_smoke_sandbox(...) 帶 **kwargs，或 allow_external_network 不是字面 False。"""
    if not (isinstance(node, ast.Call) and ast.unparse(node.func).endswith("enable_smoke_sandbox")):
        return False
    return any(
        keyword.arg is None
        or (
            keyword.arg == "allow_external_network"
            and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
        )
        for keyword in node.keywords
    )


_LIVE_CLI_OPT_INS = frozenset({"scripts/run_scorecard.py", "scripts/run_tool_eval_episode.py"})


# 開放值必須正好是 `LIVE_API_APPROVAL_FLAG in sys.argv`（寫反或 `or True` 都不算）。
_APPROVAL_BOUND = ast.dump(ast.parse("LIVE_API_APPROVAL_FLAG in sys.argv", mode="eval").body)


def test_only_human_gated_live_scripts_may_allow_external_network() -> None:
    opt_ins: dict[str, ast.Module] = {}
    for root in (PROJECT_ROOT / "tests", PROJECT_ROOT / "scripts"):
        for directory, dirnames, filenames in root.walk():
            dirnames[:] = [name for name in dirnames if not name.startswith((".", "__"))]
            for path in (directory / name for name in filenames if name.endswith(".py")):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source) if "enable_smoke_sandbox" in source else None
                if tree and any(map(_opts_into_external_network, ast.walk(tree))):
                    opt_ins[path.relative_to(PROJECT_ROOT).as_posix()] = tree

    opt_ins.pop("tests/unit/test_smoke_sandbox_governance.py", None)  # 本檔自己的 oracle
    assert {"tests/smoke/manual/live_chat_probe.py", *_LIVE_CLI_OPT_INS} <= set(opt_ins)
    for item, tree in opt_ins.items():
        assert item.startswith("tests/smoke/manual/") or item in _LIVE_CLI_OPT_INS, item
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        gates = [
            node.lineno for node in calls if ast.unparse(node.func) == "preflight_live_api_cli"
        ]
        opened = [node.lineno for node in calls if _opts_into_external_network(node)]
        assert gates and min(gates) < min(opened), f"{item} 必須先過人類同意關卡才開放外網"
        local_modes = [
            kw
            for n in calls
            if ast.unparse(n.func) == "preflight_live_api_cli"
            for kw in n.keywords
            if kw.arg == "local_only_flags"
        ]
        opens = [kw for n in calls if _opts_into_external_network(n) for kw in n.keywords]
        bound = all(ast.dump(kw.value) == _APPROVAL_BOUND for kw in opens if kw.arg != "name")
        assert not local_modes or bound, f"{item} 有免同意的本機模式，開放外網須綁核准旗標"


def test_smoke_sandbox_blocks_external_network_unless_opted_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sandbox = _load_module("smoke_network_opt_in", SMOKE_DIR / "lib" / "sandbox_env.py")
    option = inspect.signature(sandbox.enable_smoke_sandbox).parameters["allow_external_network"]
    assert (option.kind, option.default) == (inspect.Parameter.KEYWORD_ONLY, False)
    assert sandbox.network_guard is sys.modules["tests.network_guard"]
    installs: list[str] = []
    monkeypatch.setattr(sandbox.network_guard, "install", lambda: installs.append("install"))
    monkeypatch.setenv("TOUKA_SMOKE_SANDBOX_OWNER", "runner")
    original_env = dict(_PROCESS_ENVIRONMENT)
    try:
        for name, allow in (("network-default", False), ("network-manual", True)):
            monkeypatch.setenv("TOUKA_SMOKE_SANDBOX_ROOT", str(tmp_path / name))
            monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
            sandbox.enable_smoke_sandbox(name, allow_external_network=allow)
            assert ("HTTPS_PROXY" in _PROCESS_ENVIRONMENT) is allow  # 封鎖模式清掉代理
    finally:
        _PROCESS_ENVIRONMENT.clear()
        _PROCESS_ENVIRONMENT.update(original_env)
    assert installs == ["install"]


def test_child_environment_scrubs_all_credential_shaped_names(tmp_path: Path) -> None:
    sandbox = _load_module("smoke_secret_scrub", SMOKE_DIR / "lib" / "sandbox_env.py")
    credential_names = {
        "OPENAI_API_KEY",
        "TAVILY_API_KEY",
        "HF_TOKEN",
        "TOUKA_AUTH_SECRET",
        "DISPATCH_TOKEN",
        "HEARTBEAT_TOKEN",
        "TOUKA_MCP_OAUTH_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "SOME_PROVIDER_CREDENTIAL",
        "MODEL_MASTER_KEY",
    }
    base_env = dict.fromkeys(credential_names, "must-not-leak")
    base_env["PATH"] = "safe-path"
    base_env["TOUKA_DATA_DIR"] = str(tmp_path / "unselected-data")
    base_env["HEARTBEAT_CONFIG_FILE"] = str(tmp_path / "unselected-heartbeat.json")

    child = sandbox.build_smoke_environment(
        tmp_path / "credential-scrub",
        base_env,
        owner="runner",
    )

    assert credential_names.isdisjoint(child)
    assert child["PATH"] == "safe-path"
    assert Path(child["TOUKA_DATA_DIR"]) == tmp_path / "credential-scrub" / "data"
    assert Path(child["HEARTBEAT_CONFIG_FILE"]) == tmp_path / "credential-scrub" / "heartbeat.json"


@pytest.mark.parametrize("module", ["ToukaHeartbeat.awaken", "awaken"])
def test_heartbeat_import_before_sandbox_is_rejected(tmp_path, monkeypatch, module):
    runner = _load_module("smoke_import_preflight", SMOKE_DIR / "run_all_smoke_tests.py")
    path = tmp_path / "smoke_synthetic.py"
    path.write_text(f"import {module}\nenable_smoke_sandbox()\nimport src.main\n", encoding="utf-8")
    monkeypatch.setattr(runner, "SMOKE_TESTS_DIR", tmp_path)
    spec = SimpleNamespace(path=path.name, execution_class="AUTOMATED_SANDBOXED", metered_api=False)
    with pytest.raises(runner.SmokeRunnerError, match="precede"):
        runner._validate_script_preflight(spec)


@pytest.mark.parametrize("existing_handler", [False, True])
def test_smoke_first_logging_event_keeps_import_file_handler_unopened(
    tmp_path, monkeypatch, existing_handler
):
    sandbox = _load_module("smoke_first_log", SMOKE_DIR / "lib" / "sandbox_env.py")
    owned_root = tmp_path / "first-log"
    isolated_logger = logging.RootLogger(logging.WARNING)
    handler = logging.NullHandler()
    if existing_handler:
        isolated_logger.addHandler(handler)
    monkeypatch.setattr(logging, "root", isolated_logger)
    original_env = dict(_PROCESS_ENVIRONMENT)
    delayed = logging.FileHandler(tmp_path / "unselected-heartbeat.log", delay=True)
    try:
        monkeypatch.setenv("TOUKA_SMOKE_SANDBOX_ROOT", str(owned_root))
        monkeypatch.setenv("TOUKA_SMOKE_SANDBOX_OWNER", "runner")
        sandbox.enable_smoke_sandbox("first-log")
        logging.basicConfig(handlers=[delayed])  # noqa: BASICCONFIG_BAN - owned first-log oracle
        logging.warning("synthetic first heartbeat logger event")
        assert delayed.stream is None
        assert not (tmp_path / "unselected-heartbeat.log").exists()
        if existing_handler:
            assert isolated_logger.handlers == [handler]
        else:
            assert len(isolated_logger.handlers) == 1
            assert isinstance(isolated_logger.handlers[0], logging.NullHandler)
    finally:
        delayed.close()
        _PROCESS_ENVIRONMENT.clear()
        _PROCESS_ENVIRONMENT.update(original_env)


def test_standalone_cleanup_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sandbox = _load_module("smoke_exit_probe", SMOKE_DIR / "lib" / "sandbox_env.py")

    class ExitCalled(BaseException):
        def __init__(self, code: int) -> None:
            self.code = code

    def fail_cleanup(_path: Path) -> None:
        message = "locked cleanup probe"
        raise OSError(message)

    def record_exit(code: int) -> None:
        raise ExitCalled(code)

    monkeypatch.setattr(sandbox, "_remove_standalone_sandbox", fail_cleanup)
    monkeypatch.setattr(sandbox.os, "_exit", record_exit)
    with pytest.raises(ExitCalled) as caught:
        sandbox._cleanup_standalone_at_exit(tmp_path)

    assert caught.value.code == 1


def test_swallowed_network_violation_fails_the_smoke_at_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sandbox = _load_module("smoke_network_exit", SMOKE_DIR / "lib" / "sandbox_env.py")
    exits: list[int] = []
    monkeypatch.setattr(sandbox.os, "_exit", exits.append)
    monkeypatch.setattr(sandbox.network_guard, "drain_violations", lambda: ["blocked -> 'x'"])
    monkeypatch.setattr(sandbox.sys, "__stderr__", sys.stdout)

    sandbox._fail_on_network_violations_at_exit()

    assert exits == [1]
    assert "blocked -> 'x'" in capsys.readouterr().out


def test_cleanup_steps_attempt_every_store_and_fail_loud() -> None:
    sandbox = _load_module("smoke_cleanup_steps", SMOKE_DIR / "lib" / "sandbox_env.py")
    calls: list[str] = []

    def fail_vector_cleanup() -> None:
        calls.append("vector")
        message = "vector cleanup failed"
        raise RuntimeError(message)

    def close_factual_store() -> None:
        calls.append("factual")

    with pytest.raises(RuntimeError, match="vector cleanup failed"):
        sandbox.run_cleanup_steps(fail_vector_cleanup, close_factual_store)

    assert calls == ["vector", "factual"]


class _DirectReturnVisitor(ast.NodeVisitor):
    def __init__(self, target: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.target = target
        self.bare_returns: list[ast.Return] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.target:
            for statement in node.body:
                self.visit(statement)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.target:
            for statement in node.body:
                self.visit(statement)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is None:
            self.bare_returns.append(node)


def test_smoke_entrypoints_do_not_return_success_after_failed_checks() -> None:
    failures: list[str] = []
    for path in sorted(SMOKE_DIR.glob("smoke_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "main" and not node.name.startswith("run_"):
                continue
            visitor = _DirectReturnVisitor(node)
            visitor.visit(node)
            failures.extend(
                f"{path.name}:{ret.lineno} {node.name}() uses bare return"
                for ret in visitor.bare_returns
            )

    assert not failures, "\n".join(failures)


def test_frontend_persistence_initializes_database_before_first_session_write() -> None:
    path = SMOKE_DIR / "smoke_frontend_persistence.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    init_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "init_db_async"
    ]
    upsert_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "session_service"
        and node.func.attr == "upsert_session"
    ]

    assert init_lines, "frontend persistence smoke must explicitly initialize its sandbox DB"
    assert upsert_lines
    assert min(init_lines) < min(upsert_lines)


def test_tool_coverage_rejects_domain_errors_and_uses_valid_agent_control_action() -> None:
    source = (SMOKE_DIR / "smoke_tool_coverage.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    domain_markers: tuple[str, ...] | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "DOMAIN_ERR_MARKERS"
            for target in node.targets
        ):
            domain_markers = ast.literal_eval(node.value)
            break

    assert '"agent_control": {"action": "get_status"}' in source
    assert domain_markers is not None
    assert {"Unknown action", "Unknown tool"}.issubset(domain_markers)
    assert "FAILURE_MARKERS = INTERNAL_ERR_MARKERS + DOMAIN_ERR_MARKERS" in source


def test_memory_smoke_mocks_embedding_and_covers_save_query_forget() -> None:
    source = (SMOKE_DIR / "smoke_memory_tools.py").read_text(encoding="utf-8")

    assert "memory_server.vector.SentenceTransformer" in source
    assert 'action="save"' in source
    assert 'action="query"' in source
    assert 'action="forget"' in source


@pytest.mark.parametrize(
    "filename",
    ["smoke_memory_tools.py", "smoke_tool_coverage.py"],
)
def test_memory_smokes_close_vector_and_factual_stores(filename: str) -> None:
    source = (SMOKE_DIR / filename).read_text(encoding="utf-8")

    assert "from memory_server.factual import FactualStore" in source
    assert "run_cleanup_steps(" in source
    assert "VectorStore.close_all_clients" in source
    assert "FactualStore.close_all_connections" in source


def test_runner_creates_unique_child_roots_and_cleans_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_module("smoke_runner", SMOKE_DIR / "run_all_smoke_tests.py")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    with runner.smoke_child_environment("smoke_chat.py") as first_env:
        first = Path(first_env["TOUKA_SMOKE_SANDBOX_ROOT"])
        assert first.is_dir()
        assert Path(first_env["TOUKA_DATA_DIR"]) == first / "data"
        assert Path(first_env["HEARTBEAT_CONFIG_FILE"]) == first / "heartbeat.json"
        assert "OPENAI_API_KEY" not in first_env
        with runner.smoke_child_environment("smoke_router.py") as second_env:
            second = Path(second_env["TOUKA_SMOKE_SANDBOX_ROOT"])
            assert second.is_dir()
            assert first != second
            assert Path(second_env["TOUKA_DATA_DIR"]) == second / "data"
            assert Path(second_env["HEARTBEAT_CONFIG_FILE"]) == second / "heartbeat.json"
        assert not second.exists()
    assert not first.exists()


def test_runner_cleanup_failure_is_not_silenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_module("smoke_runner_cleanup", SMOKE_DIR / "run_all_smoke_tests.py")

    def fail_cleanup(_path: Path) -> None:
        message = "locked sandbox"
        raise OSError(message)

    actual_cleanup = runner._remove_sandbox
    monkeypatch.setattr(runner, "_remove_sandbox", fail_cleanup)
    leaked_root: Path | None = None
    try:
        with (
            pytest.raises(RuntimeError, match="cleanup"),
            runner.smoke_child_environment("smoke_chat.py") as child_env,
        ):
            leaked_root = Path(child_env["TOUKA_SMOKE_SANDBOX_ROOT"])
    finally:
        if leaked_root is not None and leaked_root.exists():
            actual_cleanup(leaked_root)


def test_runner_uses_manifest_instead_of_glob_discovery() -> None:
    runner = _load_module("smoke_runner_manifest", SMOKE_DIR / "run_all_smoke_tests.py")
    source = (SMOKE_DIR / "run_all_smoke_tests.py").read_text(encoding="utf-8")

    assert [spec.path for spec in runner.automated_specs()] == [
        spec.path for spec in runner.AUTOMATED_SANDBOXED
    ]
    assert 'glob("smoke_*.py")' not in source
    assert "NOT RUN" in source
    assert "HUMAN APPROVAL REQUIRED" in source


def test_chat_smoke_uses_deterministic_provider_mock_and_fails_loud() -> None:
    source = (SMOKE_DIR / "smoke_chat.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert "load_dotenv" not in source
    assert "src.services.proxy_service.dispatch_request" in source
    assert "AsyncMock" in source
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        assert any(isinstance(child, ast.Raise) for child in ast.walk(node)), (
            "smoke_chat.py 不得吞掉例外後回傳 exit 0"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_drive", ["C", "Z"])
async def test_io_prison_cross_drive_uses_sandbox_bound(
    monkeypatch: pytest.MonkeyPatch, sandbox_drive: str
) -> None:
    """Run the original smoke against in-memory services; never resolve either drive."""
    sandbox_root = PureWindowsPath(f"{sandbox_drive}:/synthetic-smoke")
    guard_calls: list[tuple[str, str]] = []

    class SyntheticPathGuardError(Exception):
        pass

    def reject_escape(target: str, root: str) -> None:
        guard_calls.append((target, str(root)))
        raise SyntheticPathGuardError

    def reject_soul_write(*_args: object) -> None:
        raise PermissionError

    fake_modules = {
        "lib.sandbox_env": {"enable_smoke_sandbox": lambda _name: str(sandbox_root)},
        "memory_server.tools.core_file_ops": {
            "PathGuardError": SyntheticPathGuardError,
            "_enforce_root_bound": reject_escape,
        },
        "src.models.identity": {
            "AgentIdentity": SimpleNamespace(for_role=lambda **kwargs: SimpleNamespace(**kwargs)),
        },
        "src.services.resource_manager": {
            "resource_manager": SimpleNamespace(
                get_vault_path=lambda _vault, identity: str(sandbox_root / identity.agent_id),
                get_base_path=lambda: str(sandbox_root),
                write_file_text=reject_soul_write,
            ),
        },
    }
    for name, attributes in fake_modules.items():
        fake_module = ModuleType(name)
        fake_module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, fake_module)
    smoke = _load_module("io_prison_synthetic", SMOKE_DIR / "smoke_io_prison.py")
    monkeypatch.setattr(smoke, "sys", SimpleNamespace(platform="win32", exit=sys.exit))
    await smoke.run_io_prison_smoke_test()

    target, bound = guard_calls[-1]
    assert PureWindowsPath(bound) == sandbox_root / "Alice"
    assert PureWindowsPath(target).drive.casefold() != PureWindowsPath(bound).drive.casefold()
    assert len(guard_calls) == 6, "Keep traversal, absolute-path and cross-drive negative cases"
