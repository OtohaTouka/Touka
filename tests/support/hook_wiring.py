"""Hook 接線測試共用的假 runner 與執行器（ADR-0292）。

全部 sandbox：設定檔在 tmp_path，hook 一律用假的 http runner，不連網路、不讀 data/。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.middleware.layers import hooks as layer_module
from src.models.identity import AgentIdentity
from src.models.safety import AuthPoliciesConfig
from src.services import auto_reviewer as reviewer_module
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.auto_reviewer import AutoReviewer
from src.services.completion import CompletionResult
from src.services.hooks import background, lifecycle, tool_hooks
from src.services.hooks.runners import RunResult
from src.services.hooks.service import HookService
from src.services.permission_service import permission_service
from src.services.review.budget import _reset_review_ledger
from src.services.tool_executor import ToolExecutor
from src.utils.user_intent import chat_intent

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import pytest

SOUL = AgentIdentity.for_role(agent_id="HookWiringProbe", role="Owner", is_human=False)
WAIT_S = 5.0
HTTP_HOOK = {"type": "http", "url": "http://127.0.0.1:9/hook"}
REVIEW_ALLOW = '{"verdict": "allow", "reason": "the user asked for it"}'
REVIEW_DENY = '{"verdict": "deny", "reason": "clearly harmful"}'


@dataclass
class FakeHttp:
    """假的 http runner：記下每次的事件內容，依事件回指定的回應內容。

    ``failing`` 裡的事件回 500；``gates`` 裡的事件等到 Event 被 set 才回（內容先記下）。
    """

    bodies: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    failing: set[str] = field(default_factory=set)
    gates: dict[str, asyncio.Event] = field(default_factory=dict)

    async def __call__(self, _hook: Any, payload: bytes, **_kwargs: Any) -> RunResult:
        body = json.loads(payload)
        event = body["hook_event_name"]
        self.calls.append(body)
        if event in self.gates:
            await self.gates[event].wait()
        if event in self.failing:
            return RunResult(status="error", duration_ms=1, http_status=500, detail="HTTP 500")
        return RunResult(status="ok", duration_ms=1, http_status=200,
                         stdout=self.bodies.get(event, ""))

    def events(self) -> list[str]:
        return [call["hook_event_name"] for call in self.calls]

    def calls_for(self, event: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["hook_event_name"] == event]


@dataclass
class HookSetup:
    service: HookService
    http: FakeHttp
    path: Path

    def configure(self, groups: dict[str, list[dict[str, Any]]]) -> None:
        config = {"enabled": True, "hooks": groups, "version": "1.0.0", "author": "test"}
        self.path.write_text(json.dumps(config), encoding="utf-8")


def install_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HookSetup:
    """把接線點用的 hook 服務換成讀 tmp_path 設定檔、走假 runner 的那一份。"""
    http = FakeHttp()
    path = tmp_path / "hooks.json"
    service = HookService(config_path=lambda: path, http_runner=http)
    for module in (layer_module, tool_hooks, background, lifecycle):
        monkeypatch.setattr(module, "hook_service", service)
    monkeypatch.setattr(background, "_in_flight", set())
    return HookSetup(service=service, http=http, path=path)


def group(event_hooks: list[dict[str, Any]] | None = None, matcher: str = "") -> dict[str, Any]:
    return {"matcher": matcher, "hooks": event_hooks or [dict(HTTP_HOOK)]}


async def until(predicate: Callable[[], Any]) -> None:
    async with asyncio.timeout(WAIT_S):
        while not predicate():
            await asyncio.sleep(0.005)


class FakeCompleter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> CompletionResult:
        self.calls += 1
        return CompletionResult(text=self.text, usage={}, cost_usd=0.0, stop_reason="end",
                                error=None)


class Recorder:
    def __init__(self, result: str = '{"ok": true, "lines": 3}') -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.result


def clean_approval_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """autouse fixture 的本體：審批與審查員的狀態不留到下一個測試。"""
    approval_service.reset_for_test()
    _reset_review_ledger()
    monkeypatch.setattr(reviewer_module.audit_service, "log_decision", lambda *_a: None)
    yield
    approval_service.reset_for_test()
    _reset_review_ledger()


def use_reviewer(monkeypatch: pytest.MonkeyPatch, text: str = REVIEW_ALLOW) -> FakeCompleter:
    completer = FakeCompleter(text)
    config = {"model": "claude-haiku-5-5", "max_tokens": 300, "timeout_s": 7.0}
    monkeypatch.setattr(
        executor_module, "auto_reviewer", AutoReviewer(completer, config_loader=lambda: config)
    )
    return completer


def executor_with(monkeypatch: pytest.MonkeyPatch, registry: dict[str, Any]) -> ToolExecutor:
    """只有 ``registry`` 這些內建工具的執行器；政策用 tracked 的 ``.json.example``。"""
    example = Path(__file__).resolve().parents[2] / "ToukaProxy" / "config"
    policy = AuthPoliciesConfig.model_validate(
        json.loads((example / "auth_policies.json.example").read_text(encoding="utf-8"))
    )
    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    for name, handler in registry.items():
        if name in bindings:  # 只有終端與自帶審批的工具有綁定，其他都是一般工具
            bindings[name] = replace(bindings[name], handler=handler)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    monkeypatch.setattr(permission_service, "evaluate_policy", policy.evaluate)
    return ToolExecutor(
        protocol_manager=None, tool_registry=registry, memory_tool_registry={}, plugin_registry={},
        enforce_advertised=False,
    )


async def run_tool(
    executor: ToolExecutor, name: str, args: dict[str, Any], *,
    present: bool = True, mode: str = "Auto", approve: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """跑一次；``approve`` 不是 None 時必須跳卡並以該決定解算。"""
    ctx: dict[str, Any] = {
        "identity": SOUL, "auth_level": mode, "session_id": "s-hook",
        "user_intent": chat_intent([{"role": "user", "content": "跑一下測試"}]),
        **(extra or {}),
    }
    if present:
        ctx["resumable_stream"] = True
    queue: asyncio.Queue = asyncio.Queue()
    async with asyncio.timeout(WAIT_S):
        task = asyncio.create_task(executor.execute_to_queue(name, args, "req-hook", ctx, queue))
        if approve is not None:
            while not approval_service._pending:
                assert not task.done(), "應該跳出審批卡"
                await asyncio.sleep(0.005)
            exec_id = next(iter(approval_service._pending))
            await approval_service.resolve(exec_id, approved=approve, resolver="tester")
        result = await task
    events = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    return result, events


def cards(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "tool_approval_request"]
