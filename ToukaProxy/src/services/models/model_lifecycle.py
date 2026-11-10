"""模型快下架提醒的核心（ADR-0310）。

只根據官方明確標示：欄位讀不到、模型比對不到都當「不知道」，不提醒；也不自動換模型。三個來源：

- Anthropic、OpenAI 的模型清單（有金鑰才查）；
- Gemini 回覆裡的 ``modelStatus``（清單沒有下架日期）；
- ``complete_once`` 實際呼叫時廠商回「找不到模型」（審查員、摘要員沒有畫面可以講原因）。

Proxy 不得有主動排程（docs/rules/heartbeat.md §1）：啟動時看一次，之後由有人開畫面觸發，
成功一天查一次、失敗每家隔一小時才再試、不重試、同時只跑一次。看過的日期只留在記憶體。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, get_args

import httpx

import src.config as _config
from src.services.providers import registry
from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from src.models.model_lifecycle import ModelLifecycleStatus

logger = logging.getLogger(__name__)

MODEL_RETIRE_WARN_DAYS = 30  # 含第 30 天；巡查用 60 天，畫面用 60 天橫幅最長會掛兩個月
_SECONDS_PER_DAY = 86_400
CHECK_INTERVAL_S = _SECONDS_PER_DAY
FAILURE_COOLDOWN_S = 3_600
LIST_TIMEOUT_S = 15.0
ANTHROPIC_PAGE_SIZE = 1_000  # 官方上限；只送真金鑰驗過的參數（ADR-0310 為什麼不帶 lifecycle）
ANTHROPIC_MAX_PAGES = 5
MAX_MISSING_MODELS = 64  # 「找不到模型」最多記幾個（禁令 18）
_ANTHROPIC_VERSION = "2023-06-01"

LifecycleState = Literal["active", "deprecated", "retired"]
_LIFECYCLE_STATES: tuple[str, ...] = get_args(LifecycleState)
_GEMINI_STAGES: dict[str, LifecycleState] = {"LEGACY": "deprecated", "RETIRED": "retired"}
_GEMINI_ACTIVE_STAGES = ("EXPERIMENTAL", "PREVIEW", "STABLE")
_DATED_SUFFIX = re.compile(r"-\d{8}")  # Anthropic 舊型號：別名＋-YYYYMMDD


@dataclass(frozen=True, slots=True)
class ModelLifecycle:
    """官方對一個模型的標示。"""

    name: str
    state: LifecycleState
    retires_at: int | None = None


@dataclass(frozen=True, slots=True)
class ModelNotice:
    """這個模型要提醒什麼。"""

    status: ModelLifecycleStatus
    retires_at: int | None = None


def normalize_model_name(name: str) -> str:
    """設定裡的寫法 → 廠商清單的寫法。"""
    return name.strip().lower().removeprefix("models/").removesuffix("-thinking")


def _epoch(value: object) -> int | None:
    """RFC 3339 或 ``YYYY-MM-DD`` → UNIX 秒（沒有時區當 UTC）；看不懂回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp())
    except (ValueError, OverflowError):
        return None


def _listed(body: object) -> list[tuple[str, dict[str, Any]]]:
    """清單回應裡有名字的項目；形狀不對回空。"""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    return [(item["id"], item) for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]]


def parse_anthropic_models(body: object) -> list[ModelLifecycle]:
    """``lifecycle`` 才是下架的依據；日期讀不到就當日期未定。"""
    return [ModelLifecycle(name, item["lifecycle"], _epoch(item.get("retires_at")))
            for name, item in _listed(body) if item.get("lifecycle") in _LIFECYCLE_STATES]


def parse_openai_models(body: object) -> list[ModelLifecycle]:
    """只有 ``shutdown_date``：null＝沒排定；欄位不見或格式壞＝不知道，不能當成要下架。"""
    records: list[ModelLifecycle] = []
    for name, item in _listed(body):
        if "shutdown_date" not in item:
            continue
        if item["shutdown_date"] is None:
            records.append(ModelLifecycle(name, "active"))
        elif (retires_at := _epoch(item["shutdown_date"])) is not None:
            records.append(ModelLifecycle(name, "deprecated", retires_at))
    return records


def parse_gemini_status(model: str, raw: object) -> ModelLifecycle | None:
    """Gemini 回覆的 ``modelStatus``；沒帶或看不懂回 None。

    官方的 ``retirementTime`` 是「模型將下架的時間」，沒有限定階段，所以任何階段帶了日期都算。
    """
    status = raw.get("modelStatus") if isinstance(raw, dict) else None
    if not isinstance(status, dict):
        return None
    stage = status.get("modelStage")
    retires_at = _epoch(status.get("retirementTime"))
    state = _GEMINI_STAGES.get(stage) if isinstance(stage, str) else None
    if state is not None:
        return ModelLifecycle(model, state, retires_at)
    if retires_at is not None:
        return ModelLifecycle(model, "deprecated", retires_at)
    return ModelLifecycle(model, "active") if stage in _GEMINI_ACTIVE_STAGES else None


def _status_of(record: ModelLifecycle, now: float) -> ModelLifecycleStatus | None:
    if record.state == "retired":
        return "retired"
    if record.state != "deprecated":
        return None  # 官方：下架由 lifecycle 決定，active 就算帶過期日期也不算
    if record.retires_at is None:
        return "date_unknown"
    if record.retires_at <= now:
        return "overdue"
    within_window = record.retires_at - now <= MODEL_RETIRE_WARN_DAYS * _SECONDS_PER_DAY
    return "retiring" if within_window else None


async def _list_anthropic(client: httpx.AsyncClient, key: str) -> list[ModelLifecycle]:
    headers = {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION}
    params: dict[str, Any] = {"limit": ANTHROPIC_PAGE_SIZE}
    records: list[ModelLifecycle] = []
    for _ in range(ANTHROPIC_MAX_PAGES):
        response = await client.get(
            f"{_config.ANTHROPIC_API_BASE_URL}/v1/models", headers=headers, params=params
        )
        response.raise_for_status()
        body = response.json()
        records += parse_anthropic_models(body)
        cursor = body.get("last_id") if isinstance(body, dict) and body.get("has_more") else None
        if not cursor:
            break
        params = {**params, "after_id": cursor}
    return records


async def _list_openai(client: httpx.AsyncClient, key: str) -> list[ModelLifecycle]:
    response = await client.get(
        f"{_config.OPENAI_RESPONSES_BASE_URL}/models", headers={"Authorization": f"Bearer {key}"}
    )
    response.raise_for_status()
    return parse_openai_models(response.json())


def _always() -> bool:
    return True


def _openai_is_official() -> bool:
    """``OPENAI_BASE_URL`` 被改過時金鑰多半是中轉站的，不能送去官方；中轉站的回應也不是官方標示。"""
    return _config.OPENAI_BASE_URL.rstrip("/") == _config.OPENAI_RESPONSES_BASE_URL


@dataclass(frozen=True, slots=True)
class _Source:
    provider_id: str  # Provider Registry 的 id
    fetch: Callable[[httpx.AsyncClient, str], Awaitable[list[ModelLifecycle]]]
    usable: Callable[[], bool] = _always


_SOURCES = (
    _Source("anthropic", _list_anthropic),
    _Source("openai", _list_openai, _openai_is_official),
)


class ModelLifecycleService:
    """記下官方標示與節流狀態（只在記憶體，重啟後重查）。"""

    def __init__(self) -> None:
        self._records: dict[str, ModelLifecycle] = {}
        self._missing: dict[str, None] = {}  # 用插入順序丟掉最舊的
        self._checked_at: dict[str, float] = {}
        self._failed_at: dict[str, float] = {}
        self._checking = False

    @property
    def checking(self) -> bool:
        """背景正在查清單。"""
        return self._checking

    def trigger_check(self) -> None:
        """該查的廠商丟到背景查，不等結果；關掉設定、沒金鑰、還沒到時間都不查。"""
        if self._checking or not _config.MODEL_LIFECYCLE_CHECK_ENABLED:
            return
        now = get_current_epoch()
        due = [(source, key) for source in _SOURCES
               if source.usable() and (key := registry.get_key_by_id(source.provider_id))
               and self._is_due(source.provider_id, now)]
        if due:
            self._checking = True
            spawn_logged(self._check(due), name="model_lifecycle_check")

    def _is_due(self, provider_id: str, now: float) -> bool:
        checked = self._checked_at.get(provider_id)
        failed = self._failed_at.get(provider_id)
        return (checked is None or now - checked >= CHECK_INTERVAL_S) and (
            failed is None or now - failed >= FAILURE_COOLDOWN_S
        )

    async def _check(self, due: list[tuple[_Source, str]]) -> None:
        try:
            # 禁令 22：金鑰不跟著轉址送出去
            async with httpx.AsyncClient(follow_redirects=False, timeout=LIST_TIMEOUT_S) as client:
                for source, key in due:
                    await self._check_source(client, source, key)
        finally:
            self._checking = False

    async def _check_source(self, client: httpx.AsyncClient, source: _Source, key: str) -> None:
        now = get_current_epoch()
        try:
            records = await source.fetch(client, key)
        except Exception as error:  # 任何失敗都算這家查失敗、進冷卻；禁令 23：只記類別名
            self._failed_at[source.provider_id] = now
            logger.warning("Model list check failed for %s: %s",
                           source.provider_id, type(error).__name__)
            return
        self._checked_at[source.provider_id] = now
        if not records:  # 欄位被改掉時提醒不會跳，至少紀錄檔要看得到
            logger.warning("Model list for %s had no readable lifecycle data", source.provider_id)
        for record in records:
            self.record(record)

    def record(self, lifecycle: ModelLifecycle) -> None:
        """記下官方標示；同一個模型以最新的為準。"""
        self._records[normalize_model_name(lifecycle.name)] = lifecycle

    def record_not_found(self, model: str) -> None:
        """實際呼叫時廠商回「找不到模型」。"""
        name = normalize_model_name(model)
        self._missing.pop(name, None)
        self._missing[name] = None
        if len(self._missing) > MAX_MISSING_MODELS:
            del self._missing[next(iter(self._missing))]

    def clear_not_found(self, model: str) -> None:
        """同一個模型後來呼叫成功。"""
        self._missing.pop(normalize_model_name(model), None)

    def _lookup(self, name: str) -> ModelLifecycle | None:
        """完全相同優先；否則別名只有一個帶日期的 ID 符合才對應（兩個以上不猜）。"""
        exact = self._records.get(name)
        if exact is not None:
            return exact
        dated = [record for key, record in self._records.items()
                 if key.startswith(name) and _DATED_SUFFIX.fullmatch(key[len(name):])]
        return dated[0] if len(dated) == 1 else None

    def notice_for(self, model: str) -> ModelNotice | None:
        """這個模型要提醒什麼；不用提醒或不知道回 None。"""
        name = normalize_model_name(model)
        if name in self._missing:
            return ModelNotice("not_found")
        record = self._lookup(name)
        status = None if record is None else _status_of(record, get_current_epoch())
        return None if status is None else ModelNotice(status, record.retires_at)


model_lifecycle_service = ModelLifecycleService()
