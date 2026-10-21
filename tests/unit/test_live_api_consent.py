"""計量／外部模型 API 的人工核准閘（ADR-0214）。

本檔只測純政策與 CLI 接線；不得載入金鑰或發出任何網路請求。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "tests" / "live_api_policy.py"
APPROVAL_FLAG = "--human-approved-live-api"
_PROCESS_ENVIRONMENT = os.environ  # noqa: ENV_SSOT_VIOLATION - pytest sandbox contract


class _UnexpectedProviderHttp(AssertionError):
    def __init__(self) -> None:
        super().__init__("HTTP transport must not be reached")


def _policy():
    return importlib.import_module("tests.live_api_policy")


def test_live_api_policy_is_the_machine_readable_ssot() -> None:
    assert POLICY_PATH.is_file(), "缺少 tests/live_api_policy.py 人工 live API 政策 SSOT"

    operations = {item.operation_id: item for item in _policy().LIVE_API_OPERATIONS}
    assert set(operations) == {
        "creation_model_judge",
        "manual_live_chat_probe",
        "manual_memory_eval_probe",
        "manual_thinking_latency_probe",
        "scorecard_cli",
        "scorecard_debug_endpoint",
        "tool_eval_baseline_list_models",
        "tool_eval_baseline_record",
        "tool_eval_live_episode",
    }
    assert operations["tool_eval_baseline_list_models"].metered_api is False
    assert all(item.requires_human_approval for item in operations.values())
    assert all(item.automated is False for item in operations.values())


def test_provider_request_budget_blocks_before_unapproved_extra_call() -> None:
    policy = _policy()
    budget = policy.LiveApiCallBudget(max_requests=2)

    assert budget.consume() == 1
    assert budget.consume() == 2
    with pytest.raises(policy.LiveApiRequestLimitExceeded, match="limit exceeded"):
        budget.consume()
    assert budget.used_requests == 2


def test_missing_human_approval_fails_closed() -> None:
    policy = _policy()

    with pytest.raises(policy.LiveApiApprovalRequired, match="人類明確同意"):
        policy.require_live_api_approval(
            approved=False,
            scope=policy.LiveApiScope(
                operation_id="manual_live_chat_probe",
                provider="openai",
                model="gpt-test",
                limits=("provider_requests=1", "max_tokens=64"),
                data_scope="fixed synthetic prompt",
                write_scope="temporary sandbox only",
            ),
        )


def test_issued_live_api_capability_is_bound_to_the_exact_scope() -> None:
    policy = _policy()
    approved_scope = policy.LiveApiScope(
        operation_id="manual_live_chat_probe",
        provider="openai",
        model="gpt-test",
        limits=("provider_requests=1", "max_tokens=64", "retry=0", "fallback=0"),
        data_scope="one fixed synthetic prompt; no real conversation data",
        write_scope="temporary sandbox only",
    )
    approval = policy.issue_live_api_approval(approved=True, scope=approved_scope)

    policy.validate_live_api_approval(approval, expected_scope=approved_scope)
    with pytest.raises(policy.LiveApiApprovalScopeMismatch):
        policy.validate_live_api_approval(
            approval,
            expected_scope=policy.LiveApiScope(
                operation_id="manual_live_chat_probe",
                provider="openai",
                model="gpt-test",
                limits=("provider_requests=1", "max_tokens=65", "retry=0", "fallback=0"),
                data_scope="one fixed synthetic prompt; no real conversation data",
                write_scope="temporary sandbox only",
            ),
        )


def test_provider_credential_environment_names_are_policy_ssot() -> None:
    assert _policy().PROVIDER_CREDENTIAL_ENV_VARS == (
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GROK_API_KEY",
        "LITELLM_API_KEY",
        "OPENAI_API_KEY",
        "TAVILY_API_KEY",
        "XAI_API_KEY",
    )


def test_every_registry_provider_key_is_cleared_by_the_pytest_policy() -> None:
    registry = importlib.import_module("src.services.providers.registry")
    key_attrs = {name for spec in registry.PROVIDER_SPECS for name in spec.key_attrs}
    assert key_attrs
    assert key_attrs <= set(_policy().PROVIDER_CREDENTIAL_ENV_VARS)


def test_approved_scope_is_complete_and_never_contains_secrets() -> None:
    policy = _policy()
    rendered = policy.require_live_api_approval(
        approved=True,
        scope=policy.LiveApiScope(
            operation_id="manual_live_chat_probe",
            provider="openai",
            model="gpt-test",
            limits=("provider_requests=1", "max_tokens=64", "retry=0", "fallback=0"),
            data_scope="fixed synthetic prompt",
            write_scope="temporary sandbox only",
        ),
    )

    assert "operation=manual_live_chat_probe" in rendered
    assert "provider=openai" in rendered
    assert "model=gpt-test" in rendered
    assert "provider_requests=1" in rendered
    assert "retry=0" in rendered
    assert "fallback=0" in rendered
    assert "API_KEY" not in rendered
    assert "Bearer" not in rendered


@pytest.mark.parametrize("local_flag", ["--dry-run", "--list-cases"])
def test_early_preflight_allows_only_declared_local_modes(local_flag: str) -> None:
    policy = _policy()
    policy.preflight_live_api_cli(
        [local_flag],
        local_only_flags=("--dry-run", "--list-cases"),
    )


@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_early_preflight_exits_help_before_product_imports(help_flag: str) -> None:
    policy = _policy()

    with pytest.raises(SystemExit) as exc_info:
        policy.preflight_live_api_cli([help_flag])

    assert exc_info.value.code == 0


def test_early_preflight_blocks_external_model_listing_without_approval() -> None:
    policy = _policy()

    with pytest.raises(SystemExit, match="human-approved-live-api"):
        policy.preflight_live_api_cli(
            ["--list-models"],
            local_only_flags=("--dry-run",),
        )


def test_early_preflight_accepts_current_invocation_flag() -> None:
    policy = _policy()
    policy.preflight_live_api_cli(
        ["--model", "gpt-test", APPROVAL_FLAG],
        local_only_flags=(),
    )


@pytest.mark.parametrize(
    ("relative_path", "first_risky_import", "runtime_budget_symbol"),
    [
        ("scripts/run_scorecard.py", "from dotenv import load_dotenv", "LiveApiCallBudget"),
        ("scripts/record_tool_eval_baseline.py", "import httpx", ""),
        (
            "scripts/run_tool_eval_episode.py",
            "from tests.tool_eval import live_episode",
            "LiveEpisodeLimits",
        ),
    ],
)
def test_live_cli_preflight_precedes_provider_capable_imports(
    relative_path: str,
    first_risky_import: str,
    runtime_budget_symbol: str,
) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")

    assert APPROVAL_FLAG in source
    assert "preflight_live_api_cli(" in source
    assert source.index("preflight_live_api_cli(") < source.index(first_risky_import)
    assert "require_live_api_approval(" in source
    if runtime_budget_symbol:
        assert "max-provider-requests" in source
        assert runtime_budget_symbol in source
        assert "provider_requests<=" in source
        assert "max_retries=0" in source
        assert "provider_retry=0" in source
        assert "main_loop_send_attempts=1" in source  # F1：揭露照實寫出主迴圈只送 1 次
        assert "Gemini adapter 目前含內建 retry" in source
    else:
        assert "provider_requests<=" in source


async def test_scorecard_cli_budget_turns_off_main_loop_retries(monkeypatch) -> None:
    """F1：揭露寫 provider_retry=0，裝預算時就要把主迴圈的共用重試開關設成只送 1 次。"""
    react_core = importlib.import_module("src.handlers.react_core")
    adapters = [
        importlib.import_module("src.handlers.adapters.claude_adapter").ClaudeAdapter,
        importlib.import_module("src.handlers.adapters.openai_adapter").OpenAIAdapter,
    ]
    for adapter in adapters:  # 先快照，teardown 還原產品類別
        monkeypatch.setattr(adapter, "send_round", adapter.send_round)
    monkeypatch.setattr(react_core, "MAX_SEND_ATTEMPTS", react_core.MAX_SEND_ATTEMPTS)
    assert react_core.MAX_SEND_ATTEMPTS == 3
    cli = runpy.run_path(str(ROOT / "scripts" / "run_scorecard.py"))

    cli["_install_provider_request_budget"](2)

    assert react_core.MAX_SEND_ATTEMPTS == 1


async def test_tool_eval_episode_cli_turns_off_main_loop_retries(monkeypatch) -> None:
    """F1：episode CLI 跑之前把主迴圈設成只送 1 次（429／5xx 不在上限內重送）。"""
    react_core = importlib.import_module("src.handlers.react_core")
    live_episode = importlib.import_module("tests.tool_eval.live_episode")
    monkeypatch.setattr(react_core, "MAX_SEND_ATTEMPTS", react_core.MAX_SEND_ATTEMPTS)
    seen: list[int] = []

    async def _fake_episode(*_args, **_kwargs):
        seen.append(react_core.MAX_SEND_ATTEMPTS)
        return SimpleNamespace()

    monkeypatch.setattr(live_episode, "run_case_live_episode", _fake_episode)
    cli = runpy.run_path(str(ROOT / "scripts" / "run_tool_eval_episode.py"))

    await cli["_run_bounded_episode"](
        case=SimpleNamespace(id="synthetic"),
        model="gpt-test",
        agent_id="DebugAgent",
        artifact_root=None,
        provider="openai",
        max_provider_requests=1,
        max_tokens=64,
        timeout_seconds=5.0,
    )

    assert seen == [1]


@pytest.mark.parametrize(
    "relative_path",
    ["scripts/record_tool_eval_baseline.py"],
)
def test_manual_cli_product_imports_use_config_write_copy(relative_path: str) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")

    preflight = source.index("preflight_live_api_cli(")
    config_copy = source.index("shutil.copytree(")
    assert preflight < config_copy
    assert '_process_environment["TOUKA_CONFIG_DIR"]' in source
    assert '_process_environment["TOUKA_BOOTSTRAP_ENABLED"] = "false"' in source
    assert '_process_environment["ENABLE_DYNAMIC_MODELS"] = "false"' in source
    assert '_process_environment["TOUKA_EVAL_JUDGE_ENABLED"] = "false"' in source


@pytest.mark.parametrize(
    "relative_path",
    ["scripts/run_scorecard.py", "scripts/run_tool_eval_episode.py"],
)
def test_tool_executing_live_clis_move_all_write_roots_to_temp_sandbox(
    relative_path: str,
) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")

    assert source.index("enable_smoke_sandbox(") < source.index("from src")
    assert 'os.environ["HOST_WORKSPACE_ROOT"]' not in source
    assert 'os.environ["WORKSPACE_ROOT"]' not in source
    assert 'write_scope="temporary sandbox only"' in source


def test_live_episode_artifact_root_is_confined_to_its_temp_sandbox(
    tmp_path: Path,
) -> None:
    source = (ROOT / "scripts" / "run_tool_eval_episode.py").read_text(encoding="utf-8")

    assert "_resolve_sandbox_artifact_root(" in source
    assert "artifact_root must be relative to the live sandbox" in source
    assert "artifact_root escaped the live sandbox" in source

    namespace = runpy.run_path(str(ROOT / "scripts" / "run_tool_eval_episode.py"))
    resolve = namespace["_resolve_sandbox_artifact_root"]
    assert resolve("artifacts", str(tmp_path)) == (tmp_path / "artifacts").resolve()
    with pytest.raises(ValueError, match="must be relative"):
        resolve(str(tmp_path / "absolute"), str(tmp_path))
    with pytest.raises(ValueError, match="escaped"):
        resolve("../escape", str(tmp_path))


def test_live_episode_overrides_per_turn_token_limit_before_dispatch() -> None:
    source = (ROOT / "tests" / "tool_eval" / "live_episode.py").read_text(encoding="utf-8")
    cli_source = (ROOT / "scripts" / "run_tool_eval_episode.py").read_text(encoding="utf-8")
    assert "request.max_tokens = limits.max_tokens" in source
    assert "max_tokens_per_model_turn=" in cli_source


def test_pytest_automation_forces_metered_features_off_before_app_import() -> None:
    source = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    app_import = source.index("from src.main import app")

    assert source.index('os.environ["TOUKA_ENV_FILE"]') < app_import
    assert source.index("for _credential_name in PROVIDER_CREDENTIAL_ENV_VARS") < app_import
    assert source.index('os.environ["TOUKA_EVAL_JUDGE_ENABLED"] = "false"') < app_import
    assert source.index('os.environ["ENABLE_DYNAMIC_MODELS"] = "false"') < app_import
    assert source.index("network_guard.install()") < source.index("from src")
    assert importlib.import_module("tests.network_guard").is_installed()
    assert source.index("clear_proxy_environment(") < app_import
    assert _PROCESS_ENVIRONMENT.get("NO_PROXY") == "*" and "HTTPS_PROXY" not in _PROCESS_ENVIRONMENT
    for offline_flag in ("HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE"):
        assert source.index(f'"{offline_flag}"') < app_import
        assert _PROCESS_ENVIRONMENT[offline_flag] == "1"
    assert _PROCESS_ENVIRONMENT["TOUKA_EVAL_JUDGE_ENABLED"] == "false"
    assert _PROCESS_ENVIRONMENT["ENABLE_DYNAMIC_MODELS"] == "false"

    env_file = Path(_PROCESS_ENVIRONMENT["TOUKA_ENV_FILE"]).resolve()
    assert env_file.parent == Path(_PROCESS_ENVIRONMENT["MEMORY_APP_DATA_DIR"]).resolve()
    assert env_file.is_file()
    assert all(_PROCESS_ENVIRONMENT[name] == "" for name in _policy().PROVIDER_CREDENTIAL_ENV_VARS)


def test_manual_chat_probe_is_sandboxed_and_strictly_bounded() -> None:
    path = ROOT / "tests" / "smoke" / "manual" / "live_chat_probe.py"
    assert path.is_file()
    source = path.read_text(encoding="utf-8")

    assert APPROVAL_FLAG in source
    assert source.index("preflight_live_api_cli(") < source.index("from dotenv import load_dotenv")
    assert source.index("enable_smoke_sandbox(") < source.index("from src")
    assert "provider_requests=1" in source
    assert "retry=0" in source
    assert "fallback=0" in source
    assert "MAX_LIVE_TOKENS = 128" in source
    assert "is_toukasoul=False" in source
    assert "LiveApiCallBudget" in source


def test_manual_memory_eval_probe_is_sandboxed_and_strictly_bounded() -> None:
    path = ROOT / "tests" / "smoke" / "manual" / "memory_eval_probe.py"
    assert path.is_file()
    source = path.read_text(encoding="utf-8")

    assert APPROVAL_FLAG in source
    assert source.index("preflight_live_api_cli(") < source.index("from dotenv import load_dotenv")
    assert source.index("_resolve_env_file(sys.argv[1:])") < source.index("from dotenv import")
    assert source.index("enable_smoke_sandbox(") < source.index("from src")
    assert source.index("enable_smoke_sandbox(") < source.index("from tests.memory_eval")
    assert "_PROBE_SEND_ATTEMPTS = 1" in source
    assert "react_core.MAX_SEND_ATTEMPTS = _PROBE_SEND_ATTEMPTS" in source
    for adapter in ("ClaudeAdapter", "OpenAIAdapter", "ResponsesAdapter", "GeminiAdapter"):
        assert f"{adapter}.send_round = " in source  # 四家 send_round 都先核銷預算
    assert "max_retries=0" in source
    assert "provider_retry=0" in source
    assert "fallback=0" in source
    assert "LiveApiCallBudget" in source
    assert 'operation_id="manual_memory_eval_probe"' in source
    assert 'write_scope="temporary sandbox only"' in source


def test_memory_eval_probe_is_never_auto_discovered() -> None:
    """人工 probe：pytest 只收 test_*.py、smoke runner 只跑 manifest 的自動清單。"""
    manifest = importlib.import_module("tests.smoke.manifest")
    automated = {spec.path for spec in manifest.AUTOMATED_SANDBOXED}
    manual = {spec.path: spec for spec in manifest.MANUAL_LIVE_SPECS}

    assert "manual/memory_eval_probe.py" in manual
    assert "manual/memory_eval_probe.py" not in automated
    spec = manual["manual/memory_eval_probe.py"]
    assert (spec.execution_class, spec.data_access, spec.metered_api) == (
        "MANUAL_LIVE",
        "TEMP_SANDBOX",
        True,
    )
    assert not Path("memory_eval_probe.py").name.startswith(("test_", "smoke_"))


def test_manual_chat_probe_requires_the_fixed_success_ack() -> None:
    probe = importlib.import_module("tests.smoke.manual.live_chat_probe")
    success = SimpleNamespace(
        body=json.dumps({"choices": [{"message": {"content": "LIVE_PROBE_OK"}}]}).encode()
    )
    wrong_answer = SimpleNamespace(
        body=json.dumps({"choices": [{"message": {"content": "something else"}}]}).encode()
    )
    provider_error = SimpleNamespace(
        body=json.dumps({"choices": [{"message": {"content": "❌ API 發生錯誤"}}]}).encode()
    )

    assert probe._decode_response(success)["choices"]
    with pytest.raises(RuntimeError, match="expected acknowledgement"):
        probe._decode_response(wrong_answer)
    with pytest.raises(RuntimeError, match="error response"):
        probe._decode_response(provider_error)


def test_scorecard_blocks_non_debug_artifact_and_unbounded_search_scenarios() -> None:
    source = (ROOT / "scripts" / "run_scorecard.py").read_text(encoding="utf-8")

    assert '_BLOCKED_SCENARIOS = frozenset({"docgen_xlsx", "web_search"})' in source
    assert 'write_scope="temporary sandbox only"' in source


def test_baseline_cli_has_bounded_repetitions_and_output_tokens() -> None:
    cli = runpy.run_path(str(ROOT / "scripts" / "record_tool_eval_baseline.py"))

    assert cli["_bounded_repetition"]("4") == 4
    assert cli["_bounded_tokens"]("512") == 512
    with pytest.raises(argparse.ArgumentTypeError):
        cli["_bounded_repetition"]("5")
    with pytest.raises(argparse.ArgumentTypeError):
        cli["_bounded_tokens"]("513")


def test_baseline_list_models_rejects_missing_budget_before_provider_lookup() -> None:
    policy = _policy()
    cli = runpy.run_path(str(ROOT / "scripts" / "record_tool_eval_baseline.py"))

    with pytest.raises(policy.LiveApiRequestBudgetRequired):
        cli["_list_models"]("openai", None)


def test_baseline_runtime_budget_is_consumed_immediately_before_post(monkeypatch) -> None:
    policy = _policy()
    lso = importlib.import_module("tests.tool_eval.live_select_only")
    post_calls: list[dict[str, object]] = []

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"choices": []}

    def _fake_post(*_args, **kwargs):
        post_calls.append(kwargs)
        return _Response()

    monkeypatch.setattr(lso.httpx, "post", _fake_post)
    budget = policy.LiveApiCallBudget(max_requests=1)
    payload = lso._build_payload(
        "gpt-test",
        [{"role": "user", "content": "fixed"}],
        [],
        max_tokens=64,
    )

    lso._post_json("https://provider.invalid", "secret", payload, request_budget=budget)
    with pytest.raises(policy.LiveApiRequestLimitExceeded):
        lso._post_json("https://provider.invalid", "secret", payload, request_budget=budget)

    assert payload["max_tokens"] == 64
    assert len(post_calls) == 1


def test_provider_transport_rejects_missing_budget_before_http(monkeypatch) -> None:
    policy = _policy()
    lso = importlib.import_module("tests.tool_eval.live_select_only")

    def _unexpected_post(*_args, **_kwargs):
        raise _UnexpectedProviderHttp

    monkeypatch.setattr(lso.httpx, "post", _unexpected_post)
    payload = lso._build_payload(
        "gpt-test",
        [{"role": "user", "content": "fixed"}],
        [],
        max_tokens=64,
    )

    with pytest.raises(policy.LiveApiRequestBudgetRequired):
        lso._post_json(
            "https://provider.invalid",
            "secret",
            payload,
            request_budget=None,
        )


def test_provider_transport_rejects_missing_token_cap_before_http(monkeypatch) -> None:
    policy = _policy()
    lso = importlib.import_module("tests.tool_eval.live_select_only")

    def _unexpected_post(*_args, **_kwargs):
        raise _UnexpectedProviderHttp

    monkeypatch.setattr(lso.httpx, "post", _unexpected_post)
    with pytest.raises(policy.InvalidLiveApiTokenLimit):
        lso._post_json(
            "https://provider.invalid",
            "secret",
            {"model": "gpt-test", "messages": []},
            request_budget=policy.LiveApiCallBudget(max_requests=1),
        )


async def test_manual_chat_execution_requires_issued_scope_before_dispatch(monkeypatch) -> None:
    policy = _policy()
    probe = importlib.import_module("tests.smoke.manual.live_chat_probe")
    installed: list[str] = []
    monkeypatch.setattr(
        probe,
        "_install_one_request_bound",
        lambda provider: installed.append(provider),
    )

    with pytest.raises(policy.LiveApiApprovalRequired):
        await probe.run_live_chat_probe(
            model="gpt-test",
            max_tokens=64,
            provider="openai",
            approval=None,
        )

    assert installed == []


async def test_manual_chat_execution_rejects_capability_for_a_different_limit(
    monkeypatch,
) -> None:
    policy = _policy()
    probe = importlib.import_module("tests.smoke.manual.live_chat_probe")
    approval = policy.issue_live_api_approval(
        approved=True,
        scope=probe._live_chat_scope(
            model="gpt-test",
            max_tokens=63,
            provider="openai",
        ),
    )
    installed: list[str] = []
    monkeypatch.setattr(
        probe,
        "_install_one_request_bound",
        lambda provider: installed.append(provider),
    )

    with pytest.raises(policy.LiveApiApprovalScopeMismatch):
        await probe.run_live_chat_probe(
            model="gpt-test",
            max_tokens=64,
            provider="openai",
            approval=approval,
        )

    assert installed == []
