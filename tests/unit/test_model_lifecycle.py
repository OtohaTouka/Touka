"""模型快下架提醒的核心（ADR-0310）。

只根據官方明確標示：讀不到的欄位當不知道、比對不到不提醒。清單查詢全用假回應（禁令 61）。
Anthropic 的假資料照 2026-11-10 用真金鑰列一次的實際形狀；OpenAI 只照官方文件，沒有實測。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from src.services.models import model_lifecycle as lifecycle
from src.services.models.model_lifecycle import (
    ANTHROPIC_MAX_PAGES,
    CHECK_INTERVAL_S,
    FAILURE_COOLDOWN_S,
    MAX_MISSING_MODELS,
    MODEL_RETIRE_WARN_DAYS,
    ModelLifecycle,
    ModelLifecycleService,
    ModelNotice,
    normalize_model_name,
    parse_anthropic_models,
    parse_gemini_status,
    parse_openai_models,
)

from tests.helpers.stream_transport import FakeUpstream, Reply, json_reply, status_reply

_NOW = 1_799_971_200  # 2027-01-15 00:00 UTC
_DAY = 86_400
_SONNET_RETIRES = 1_796_058_000  # 2026-11-30T17:00:00Z


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(now=float(_NOW))
    monkeypatch.setattr(lifecycle, "get_current_epoch", lambda: state.now)
    return state


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch, clock: SimpleNamespace) -> ModelLifecycleService:
    monkeypatch.setattr("src.config.MODEL_LIFECYCLE_CHECK_ENABLED", True, raising=False)
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    monkeypatch.setattr("src.config.OPENAI_API_KEY", "fake-openai", raising=False)
    monkeypatch.setattr("src.config.OPENAI_BASE_URL", "https://api.openai.com/v1", raising=False)
    return ModelLifecycleService()


def _anthropic_body(*models: dict[str, Any], has_more: bool = False,
                    last_id: str | None = None) -> dict[str, Any]:
    return {"data": list(models), "has_more": has_more, "first_id": None, "last_id": last_id}


def _claude(name: str, state: str = "active", retires: str | None = None) -> dict[str, Any]:
    return {"id": name, "lifecycle": state, "deprecated_at": None, "retires_at": retires,
            "display_name": name, "type": "model"}


_ANTHROPIC_OK = json_reply(_anthropic_body(_claude("claude-opus-5-5")))
_OPENAI_OK = json_reply({"object": "list", "data": [{"id": "gpt-6-luna", "shutdown_date": None}]})


async def _finish(service: ModelLifecycleService) -> None:
    async with asyncio.timeout(5):
        while service.checking:
            await asyncio.sleep(0)


async def _check(service: ModelLifecycleService) -> None:
    service.trigger_check()
    await _finish(service)


# ── 解析：讀不到就當不知道 ──────────────────────────────────


def test_anthropic_parser_reads_the_real_shape() -> None:
    body = _anthropic_body(
        {"id": "claude-sonnet-4-5-20250929", "lifecycle": "deprecated",
         "deprecated_at": "2026-09-30T18:30:00Z", "retires_at": "2026-11-30T17:00:00Z"},
        {"id": "claude-opus-5-5", "lifecycle": "active", "deprecated_at": None,
         "retires_at": None},
    )
    assert parse_anthropic_models(body) == [
        ModelLifecycle("claude-sonnet-4-5-20250929", "deprecated", _SONNET_RETIRES),
        ModelLifecycle("claude-opus-5-5", "active", None),
    ]


def test_anthropic_parser_reads_retired() -> None:
    assert parse_anthropic_models(_anthropic_body(_claude("claude-old", "retired"))) == [
        ModelLifecycle("claude-old", "retired", None)
    ]


@pytest.mark.parametrize(
    "item",
    [
        {"id": "claude-x"},  # 欄位不見
        {"id": "claude-x", "lifecycle": "sunsetting"},  # 看不懂的狀態
        {"id": "claude-x", "lifecycle": None},
        {"lifecycle": "deprecated"},  # 沒有名字
        {"id": "", "lifecycle": "deprecated"},
        {"id": 7, "lifecycle": "deprecated"},
        "claude-x",
    ],
)
def test_anthropic_parser_skips_what_it_cannot_read(item: Any) -> None:
    assert parse_anthropic_models({"data": [item]}) == []


@pytest.mark.parametrize("retires", ["soon", "", 1796058000, None])
def test_anthropic_bad_date_means_date_unknown(retires: Any) -> None:
    body = {"data": [{"id": "claude-x", "lifecycle": "deprecated", "retires_at": retires}]}
    assert parse_anthropic_models(body) == [ModelLifecycle("claude-x", "deprecated", None)]


@pytest.mark.parametrize("body", [None, [], "x", {}, {"data": None}, {"data": "x"}])
@pytest.mark.parametrize("parser", [parse_anthropic_models, parse_openai_models])
def test_parsers_survive_a_changed_response(parser: Any, body: Any) -> None:
    assert parser(body) == []


def test_openai_parser_date_and_null() -> None:
    body = {"data": [{"id": "gpt-old", "shutdown_date": "2026-11-30"},
                     {"id": "gpt-new", "shutdown_date": None}]}
    assert parse_openai_models(body) == [
        ModelLifecycle("gpt-old", "deprecated", _SONNET_RETIRES - 17 * 3600),  # 當天 00:00 UTC
        ModelLifecycle("gpt-new", "active", None),
    ]


@pytest.mark.parametrize(
    "item",
    [
        {"id": "gpt-x"},  # 欄位不見＝這家沒給，不是沒要下架
        {"id": "gpt-x", "shutdown_date": "next spring"},  # 格式壞了不能當成要下架
        {"id": "gpt-x", "shutdown_date": 1796058000},
        {"shutdown_date": "2026-11-30"},
    ],
)
def test_openai_parser_skips_what_it_cannot_read(item: Any) -> None:
    assert parse_openai_models({"data": [item]}) == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"modelStage": "LEGACY", "retirementTime": "2026-11-30T17:00:00Z"},
         ModelLifecycle("gemini-x", "deprecated", _SONNET_RETIRES)),
        ({"modelStage": "RETIRED"}, ModelLifecycle("gemini-x", "retired", None)),
        ({"modelStage": "LEGACY", "retirementTime": "someday"},
         ModelLifecycle("gemini-x", "deprecated", None)),
        ({"modelStage": "STABLE"}, ModelLifecycle("gemini-x", "active", None)),
        ({"modelStage": "PREVIEW"}, ModelLifecycle("gemini-x", "active", None)),
        ({"modelStage": "EXPERIMENTAL"}, ModelLifecycle("gemini-x", "active", None)),
        ({"modelStage": "STABLE", "retirementTime": "2026-11-30T17:00:00Z"},
         ModelLifecycle("gemini-x", "deprecated", _SONNET_RETIRES)),
        ({"modelStage": "PREVIEW", "retirementTime": "2026-11-30T17:00:00Z"},
         ModelLifecycle("gemini-x", "deprecated", _SONNET_RETIRES)),
        ({"retirementTime": "2026-11-30T17:00:00Z"},
         ModelLifecycle("gemini-x", "deprecated", _SONNET_RETIRES)),
        ({"modelStage": "STABLE", "retirementTime": "someday"},
         ModelLifecycle("gemini-x", "active", None)),
        ({"modelStage": "SOMETHING_NEW"}, None),
        ({"modelStage": ["LEGACY"]}, None),
        ({}, None),
        ("LEGACY", None),
        (None, None),
    ],
)
def test_gemini_status(status: Any, expected: ModelLifecycle | None) -> None:
    assert parse_gemini_status("gemini-x", {"modelStatus": status}) == expected


@pytest.mark.parametrize("raw", [None, [], "x", {}, {"candidates": []}])
def test_gemini_without_status_is_unknown(raw: Any) -> None:
    assert parse_gemini_status("gemini-x", raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("models/gemini-2.5-pro", "gemini-2.5-pro"),
        ("Claude-Opus-4-8", "claude-opus-4-8"),
        ("claude-opus-4-8-thinking", "claude-opus-4-8"),
        ("  gpt-6-luna ", "gpt-6-luna"),
    ],
)
def test_normalize_model_name(raw: str, expected: str) -> None:
    assert normalize_model_name(raw) == expected


# ── 提醒的邊界 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds_left", "status"),
    [
        (29 * _DAY, "retiring"),
        (30 * _DAY, "retiring"),  # 含第 30 天
        (30 * _DAY + 1, None),
        (31 * _DAY, None),
        (1, "retiring"),
        (0, "overdue"),
        (-_DAY, "overdue"),
    ],
)
def test_retirement_window(service: ModelLifecycleService, seconds_left: int,
                           status: str | None) -> None:
    retires = _NOW + seconds_left
    service.record(ModelLifecycle("claude-x", "deprecated", retires))
    expected = None if status is None else ModelNotice(status, retires)
    assert service.notice_for("claude-x") == expected


def test_warn_days_is_thirty() -> None:
    assert MODEL_RETIRE_WARN_DAYS == 30


def test_deprecated_without_a_date(service: ModelLifecycleService) -> None:
    service.record(ModelLifecycle("claude-x", "deprecated", None))
    assert service.notice_for("claude-x") == ModelNotice("date_unknown", None)


def test_retired(service: ModelLifecycleService) -> None:
    service.record(ModelLifecycle("claude-x", "retired", _NOW - _DAY))
    assert service.notice_for("claude-x") == ModelNotice("retired", _NOW - _DAY)


def test_active_never_warns_even_with_a_past_date(service: ModelLifecycleService) -> None:
    """官方：下架由 lifecycle 決定，日期可能再改。"""
    service.record(ModelLifecycle("claude-x", "active", _NOW - _DAY))
    assert service.notice_for("claude-x") is None


def test_unknown_model_is_not_a_warning(service: ModelLifecycleService) -> None:
    service.record(ModelLifecycle("claude-y", "deprecated", None))
    assert service.notice_for("claude-x") is None


def test_lookup_normalizes_the_configured_name(service: ModelLifecycleService) -> None:
    service.record(ModelLifecycle("gemini-x", "deprecated", None))
    assert service.notice_for("models/Gemini-X") == ModelNotice("date_unknown", None)


def test_stored_name_is_normalized_too(service: ModelLifecycleService) -> None:
    service.record(ModelLifecycle("Models/Claude-X", "deprecated", None))
    assert service.notice_for("claude-x") == ModelNotice("date_unknown", None)


# ── 別名窄規則 ──────────────────────────────────────────


def test_alias_maps_to_its_single_dated_id(service: ModelLifecycleService) -> None:
    service.record(ModelLifecycle("claude-haiku-4-5-20251001", "deprecated", None))
    assert service.notice_for("claude-haiku-4-5") == ModelNotice("date_unknown", None)


def test_alias_with_two_dated_ids_is_unknown(service: ModelLifecycleService) -> None:
    service.record(ModelLifecycle("claude-haiku-4-5-20251001", "deprecated", None))
    service.record(ModelLifecycle("claude-haiku-4-5-20260101", "deprecated", None))
    assert service.notice_for("claude-haiku-4-5") is None


def test_exact_name_wins_over_the_alias_rule(service: ModelLifecycleService) -> None:
    service.record(ModelLifecycle("claude-haiku-4-5", "active", None))
    service.record(ModelLifecycle("claude-haiku-4-5-20251001", "deprecated", None))
    assert service.notice_for("claude-haiku-4-5") is None


@pytest.mark.parametrize("listed", ["claude-haiku-4-5-1", "claude-haiku-4-5-extra-20251001",
                                    "xclaude-haiku-4-5-20251001"])
def test_only_an_eight_digit_suffix_is_an_alias(service: ModelLifecycleService,
                                                listed: str) -> None:
    service.record(ModelLifecycle(listed, "deprecated", None))
    assert service.notice_for("claude-haiku-4-5") is None


# ── 廠商回「找不到模型」 ────────────────────────────────────


def test_not_found_is_remembered_and_cleared(service: ModelLifecycleService) -> None:
    service.record_not_found("Models/Claude-X")
    assert service.notice_for("claude-x") == ModelNotice("not_found", None)
    service.clear_not_found("MODELS/claude-x")
    assert service.notice_for("claude-x") is None


def test_not_found_wins_over_a_future_date(service: ModelLifecycleService) -> None:
    service.record(ModelLifecycle("claude-x", "deprecated", _NOW + 10 * _DAY))
    service.record_not_found("claude-x")
    assert service.notice_for("claude-x") == ModelNotice("not_found", None)


def test_not_found_list_is_bounded(service: ModelLifecycleService) -> None:
    for index in range(MAX_MISSING_MODELS + 1):
        service.record_not_found(f"claude-{index}")
    assert service.notice_for("claude-0") is None
    assert service.notice_for(f"claude-{MAX_MISSING_MODELS}") == ModelNotice("not_found", None)


# ── 查清單 ────────────────────────────────────────────


async def test_check_asks_each_provider_once_with_its_own_key(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = FakeUpstream([_ANTHROPIC_OK, _OPENAI_OK]).install(monkeypatch)
    await _check(service)
    anthropic, openai = upstream.requests
    assert anthropic.method == "GET"
    assert anthropic.url.host == "api.anthropic.com"
    assert anthropic.url.path == "/v1/models"
    assert dict(anthropic.url.params) == {"limit": "1000"}  # 真金鑰驗過的就是這個形狀
    assert anthropic.headers["x-api-key"] == "fake-anthropic"
    assert anthropic.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in anthropic.headers
    assert openai.method == "GET"
    assert openai.url.host == "api.openai.com"
    assert openai.url.path == "/v1/models"
    assert openai.headers["authorization"] == "Bearer fake-openai"
    assert "x-api-key" not in openai.headers


async def test_check_stores_what_the_lists_say(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeUpstream([
        json_reply(_anthropic_body(_claude("claude-x", "deprecated"))),
        json_reply({"data": [{"id": "gpt-x", "shutdown_date": "2027-01-20"}]}),
    ]).install(monkeypatch)
    await _check(service)
    assert service.notice_for("claude-x") == ModelNotice("date_unknown", None)
    notice = service.notice_for("gpt-x")
    assert notice is not None
    assert notice.status == "retiring"


async def test_anthropic_pages_until_done(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = FakeUpstream([
        json_reply(_anthropic_body(_claude("claude-a", "deprecated"), has_more=True,
                                   last_id="claude-a")),
        json_reply(_anthropic_body(_claude("claude-b", "deprecated"))),
        _OPENAI_OK,
    ]).install(monkeypatch)
    await _check(service)
    assert len(upstream.requests) == 3
    assert upstream.requests[1].url.params["after_id"] == "claude-a"
    assert service.notice_for("claude-a") is not None
    assert service.notice_for("claude-b") is not None


async def test_anthropic_paging_is_capped(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    more = json_reply(_anthropic_body(_claude("claude-a"), has_more=True, last_id="claude-a"))
    upstream = FakeUpstream([more] * ANTHROPIC_MAX_PAGES + [_OPENAI_OK]).install(monkeypatch)
    await _check(service)
    assert len(upstream.requests) == ANTHROPIC_MAX_PAGES + 1


async def test_has_more_without_a_cursor_stops(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = FakeUpstream([
        json_reply(_anthropic_body(_claude("claude-a"), has_more=True)), _OPENAI_OK,
    ]).install(monkeypatch)
    await _check(service)
    assert len(upstream.requests) == 2


async def test_a_failed_later_page_changes_nothing(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeUpstream([
        json_reply(_anthropic_body(_claude("claude-a", "deprecated"), has_more=True,
                                   last_id="claude-a")),
        status_reply(500),
        _OPENAI_OK,
    ]).install(monkeypatch)
    await _check(service)
    assert service.notice_for("claude-a") is None


async def test_no_more_pages_ignores_the_cursor(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = FakeUpstream([
        json_reply(_anthropic_body(_claude("claude-a"), last_id="claude-a")), _OPENAI_OK,
    ]).install(monkeypatch)
    await _check(service)
    assert len(upstream.requests) == 2


async def test_error_status_with_a_readable_body_is_still_a_failure(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeUpstream([
        json_reply(_anthropic_body(_claude("claude-x", "deprecated")), status=500),
        json_reply({"data": [{"id": "gpt-x", "shutdown_date": "2027-01-20"}]}, status=500),
    ]).install(monkeypatch)
    await _check(service)
    assert service.notice_for("claude-x") is None
    assert service.notice_for("gpt-x") is None


async def test_redirects_are_not_followed(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """禁令 22：金鑰不能跟著轉址送出去。"""
    upstream = FakeUpstream([
        Reply(302, [b""], {"location": "https://elsewhere.test/"}), _OPENAI_OK,
    ]).install(monkeypatch)
    real_client = httpx.AsyncClient
    created: list[dict[str, Any]] = []

    def spy(**kwargs: Any) -> httpx.AsyncClient:
        created.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", spy)
    await _check(service)
    assert len(upstream.requests) == 2
    assert created
    assert all(kwargs.get("follow_redirects") is False for kwargs in created)


@pytest.mark.parametrize(
    "failure",
    [status_reply(500), status_reply(401), json_reply("not json"),
     Reply(error=httpx.ConnectError("down")), Reply(error=httpx.ReadTimeout("slow"))],
    ids=["500", "401", "bad-json", "connect", "timeout"],
)
async def test_failure_is_not_retried_and_cools_down(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch, clock: SimpleNamespace,
    failure: Reply,
) -> None:
    upstream = FakeUpstream([failure, _OPENAI_OK]).install(monkeypatch)
    await _check(service)
    assert len(upstream.requests) == 2  # 每家只送一次

    clock.now += FAILURE_COOLDOWN_S - 1
    await _check(service)
    assert len(upstream.requests) == 2  # 冷卻中，OpenAI 也還沒到一天

    upstream.replies.append(_ANTHROPIC_OK)
    clock.now += 1
    await _check(service)
    assert len(upstream.requests) == 3
    assert upstream.requests[2].headers.get("x-api-key") == "fake-anthropic"


async def test_success_waits_a_day(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch, clock: SimpleNamespace
) -> None:
    upstream = FakeUpstream([_ANTHROPIC_OK, _OPENAI_OK]).install(monkeypatch)
    await _check(service)
    clock.now += CHECK_INTERVAL_S - 1
    await _check(service)
    assert len(upstream.requests) == 2

    upstream.replies.extend([_ANTHROPIC_OK, _OPENAI_OK])
    clock.now += 1
    await _check(service)
    assert len(upstream.requests) == 4


async def test_cooldown_is_per_provider(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch, clock: SimpleNamespace
) -> None:
    upstream = FakeUpstream([_ANTHROPIC_OK, status_reply(500)]).install(monkeypatch)
    await _check(service)
    upstream.replies.append(_OPENAI_OK)
    clock.now += FAILURE_COOLDOWN_S
    await _check(service)
    assert len(upstream.requests) == 3
    assert upstream.requests[2].headers.get("authorization") == "Bearer fake-openai"


async def test_a_failure_after_a_success_still_cools_down(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch, clock: SimpleNamespace
) -> None:
    upstream = FakeUpstream([_ANTHROPIC_OK, _OPENAI_OK]).install(monkeypatch)
    await _check(service)
    upstream.replies.extend([status_reply(500), _OPENAI_OK])
    clock.now += CHECK_INTERVAL_S
    await _check(service)
    assert len(upstream.requests) == 4
    clock.now += FAILURE_COOLDOWN_S - 1
    await _check(service)
    assert len(upstream.requests) == 4


async def test_concurrent_triggers_run_once(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = FakeUpstream([_ANTHROPIC_OK, _OPENAI_OK]).install(monkeypatch)
    service.trigger_check()
    assert service.checking is True
    service.trigger_check()
    await _finish(service)
    assert len(upstream.requests) == 2
    assert service.checking is False


async def test_checking_ends_after_a_failure(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeUpstream([status_reply(500), status_reply(500)]).install(monkeypatch)
    await _check(service)
    assert service.checking is False


async def test_setting_off_means_no_request(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.config.MODEL_LIFECYCLE_CHECK_ENABLED", False, raising=False)
    upstream = FakeUpstream([]).install(monkeypatch)
    service.trigger_check()
    assert service.checking is False
    assert upstream.requests == []


async def test_provider_without_a_key_is_skipped(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "", raising=False)
    upstream = FakeUpstream([_OPENAI_OK]).install(monkeypatch)
    await _check(service)
    assert len(upstream.requests) == 1
    assert "x-api-key" not in upstream.requests[0].headers


async def test_no_keys_at_all_starts_nothing(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "", raising=False)
    monkeypatch.setattr("src.config.OPENAI_API_KEY", "", raising=False)
    upstream = FakeUpstream([]).install(monkeypatch)
    service.trigger_check()
    assert service.checking is False
    assert upstream.requests == []


async def test_openai_with_a_changed_base_url_is_skipped(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """金鑰多半是中轉站的，不能送去官方；中轉站的回應也不是官方標示。"""
    monkeypatch.setattr("src.config.OPENAI_BASE_URL", "https://relay.test/v1", raising=False)
    upstream = FakeUpstream([_ANTHROPIC_OK]).install(monkeypatch)
    await _check(service)
    assert [request.url.host for request in upstream.requests] == ["api.anthropic.com"]


async def test_trailing_slash_on_the_official_url_is_still_official(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.config.OPENAI_BASE_URL", "https://api.openai.com/v1/", raising=False)
    upstream = FakeUpstream([_ANTHROPIC_OK, _OPENAI_OK]).install(monkeypatch)
    await _check(service)
    assert len(upstream.requests) == 2
    assert upstream.requests[1].url.path == "/v1/models"  # 尾斜線不能變成 //models


async def test_checking_ends_when_the_task_is_cancelled(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = Reply(200, [asyncio.Event()], {"content-type": "application/json"})
    upstream = FakeUpstream([held]).install(monkeypatch)
    service.trigger_check()
    async with asyncio.timeout(5):
        while not upstream.requests:
            await asyncio.sleep(0)
    task = next(t for t in asyncio.all_tasks() if t.get_name() == "model_lifecycle_check")
    task.cancel()
    await asyncio.wait([task], timeout=5)
    assert service.checking is False


async def test_a_seen_date_survives_the_model_leaving_the_list(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch, clock: SimpleNamespace
) -> None:
    retires = "2027-01-20T00:00:00Z"
    upstream = FakeUpstream([
        json_reply(_anthropic_body(_claude("claude-x", "deprecated", retires))), _OPENAI_OK,
    ]).install(monkeypatch)
    await _check(service)
    upstream.replies.extend([json_reply(_anthropic_body()), _OPENAI_OK])
    clock.now += 10 * _DAY
    await _check(service)
    notice = service.notice_for("claude-x")
    assert notice is not None
    assert notice.status == "overdue"


async def test_official_rollback_clears_the_warning(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch, clock: SimpleNamespace
) -> None:
    upstream = FakeUpstream([
        json_reply(_anthropic_body(_claude("claude-x", "deprecated"))), _OPENAI_OK,
    ]).install(monkeypatch)
    await _check(service)
    upstream.replies.extend([json_reply(_anthropic_body(_claude("claude-x"))), _OPENAI_OK])
    clock.now += CHECK_INTERVAL_S
    await _check(service)
    assert service.notice_for("claude-x") is None


async def test_failure_log_has_no_key(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """禁令 23：例外文字可能帶金鑰，只記類別名。"""
    FakeUpstream([
        Reply(error=httpx.ConnectError("boom fake-anthropic")), _OPENAI_OK,
    ]).install(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger=lifecycle.__name__):
        await _check(service)
    assert "ConnectError" in caplog.text
    assert "fake-anthropic" not in caplog.text


async def test_a_list_with_nothing_readable_is_logged(
    service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """欄位被改掉時提醒不會跳，至少紀錄檔要看得到。"""
    FakeUpstream([json_reply({"data": [{"id": "claude-x"}]}), _OPENAI_OK]).install(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=lifecycle.__name__):
        await _check(service)
    assert "anthropic" in caplog.text
