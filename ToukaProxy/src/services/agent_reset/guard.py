"""DebugAgent 破壞性重置的安全閘（SSOT）。

兩道防線確保任何重置邏輯**絕不可能**被導向真實 agent（Jin/Yan/Code 等）：
① 字面硬鎖（正規化後 == "DebugAgent"）；② 目標不可擁有 soul/custom 實體目錄（真實 agent 的特徵）。
參數刻意命名 `candidate`（未經驗證的字串，尚非可信 agent_id），避免與身分物件混淆。
"""
from __future__ import annotations

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.agent_service import agent_service
from src.services.resource_manager import resource_manager
from src.utils import soul_io

ALLOWED_RESET_AGENT = "DebugAgent"
_LEGACY_PREFIX = "ToukaSoul"

# ADR-0113：有專屬重置路徑或屬系統內建、**不可由通用 per-agent 重置觸及**的代理人。
# DebugAgent 走自己的 ADR-0104 路徑（assert_resettable）；通用路徑硬性排除之。
SYSTEM_PROTECTED_AGENTS = frozenset({ALLOWED_RESET_AGENT})


def _normalize(candidate: str) -> str:
    """去 ToukaSoul 前綴（與 agent_service.identify_agent 同糾偏邏輯）並 strip。"""
    raw = (candidate or "").strip()
    return raw[len(_LEGACY_PREFIX):] if raw.startswith(_LEGACY_PREFIX) else raw


def _has_real_persona(candidate: str) -> bool:
    """該 id 是否為真實 agent（有 soul 目錄、或有 custom *config.json* 人格定義）。

    刻意用 `identify_agent`（檢查真實人格設定）＋soul 目錄，而**非**僅憑 `data/custom/<id>/`
    目錄存在——因 DebugAgent 產出（docgen/outputs）會落在 custom drive 下、建出目錄卻無
    config.json；用目錄存在判定會讓 DebugAgent 自我封鎖重置。soul 目錄本身即真實人格故保留。
    """
    try:
        if candidate in set(soul_io.list_soul_ids()):
            return True
    except Exception:  # noqa: BLE001  # 列舉失敗→無法證明非真實 agent→保守視為有（拒絕重置）
        return True
    info = agent_service.identify_agent(candidate)
    return bool(info and (info.get("is_toukasoul") or info.get("is_custom")))


def assert_resettable(candidate: str) -> str:
    """驗證 candidate 可被破壞性重置；回正規化後 id，否則 raise AUTH_UNAUTHORIZED_ACCESS。

    任一防線不過即拒絕——保證絕不誤刪真實 agent。
    """
    normalized = _normalize(candidate)
    if normalized != ALLOWED_RESET_AGENT:
        raise ToukaException(ErrorCode.AUTH_UNAUTHORIZED_ACCESS)
    if _has_real_persona(normalized):
        # DebugAgent 不該有真實人格（soul/custom config）；若有，視為身分異常 → 拒絕（安全優先）。
        raise ToukaException(ErrorCode.AUTH_UNAUTHORIZED_ACCESS)
    return normalized


def _is_real_persona_strict(candidate: str) -> bool:
    """嚴格（fail-closed）：僅在能**確認**為真實 agent（有 soul 或 custom config）時回 True。

    與 `_has_real_persona`（為 DebugAgent 守衛調校：不確定→True→保守拒絕）相反——per-agent
    重置「必須是真實 agent」，故不確定（列舉失敗）一律 False → 拒絕，避免誤對不存在的 id 動手。
    """
    try:
        if candidate in set(soul_io.list_soul_ids()):
            return True
    except Exception:  # noqa: BLE001  # 列舉失敗→無法確認→fail-closed 視為非真實 agent
        return False
    info = agent_service.identify_agent(candidate)
    return bool(info and (info.get("is_toukasoul") or info.get("is_custom")))


def assert_agent_resettable(candidate: str, actor: AgentIdentity) -> str:
    """ADR-0113：驗證 candidate 可被「per-agent 資料重置」；回正規化 id，否則 raise。

    反轉 `assert_resettable` 的人格判定——要求目標**是**真實且被擁有的非系統 agent：
      ① 非空、② 非系統保護代理人（DebugAgent…）、③ **確認為真實 agent**（fail-closed）、
      ④ 被 actor 擁有（admin 經 is_owner 絕對放行；保留檢查以利未來非-admin Owner 路徑＋稽核）。
    身分永不被觸及（soul/legacy 受 is_protected_path 物理保護、custom config 由清除原語範圍排除）。
    """
    normalized = _normalize(candidate)
    if not normalized or normalized in SYSTEM_PROTECTED_AGENTS:
        raise ToukaException(ErrorCode.AUTH_UNAUTHORIZED_ACCESS)
    if not _is_real_persona_strict(normalized):
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR, detail_override="找不到可重置的代理人",
        )
    # role 非 "Owner"（避免 is_toukasoul 的 Owner 捷徑誤判路徑）；is_owner 用 actor 判定主權。
    target = AgentIdentity(agent_id=normalized, agent_role="Base")
    if not resource_manager.is_owner(target, actor):
        raise ToukaException(ErrorCode.AUTH_UNAUTHORIZED_ACCESS)
    return normalized
