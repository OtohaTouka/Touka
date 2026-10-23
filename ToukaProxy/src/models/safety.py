"""安全性執行策略與黑白名單規則 (Safety Execution Policies).

定義黑白名單規則 (SafetyRules)、工具風險分類 (ToolRiskConfig) 與核心決策判定邏輯
(AuthPoliciesConfig)。三模式決策表（ADR-0246）：

==============  ===========  ========  ===========  ===========
mode            read_only    safe      dangerous    終端
==============  ===========  ========  ===========  ===========
Manual          ALLOW        CONFIRM   CONFIRM      CONFIRM
Auto            ALLOW        ALLOW     REVIEW       REVIEW
YOLO            ALLOW        ALLOW     ALLOW        ALLOW
==============  ===========  ========  ===========  ===========

REVIEW＝交給 Auto 審查員（U2c-3，``services/auto_reviewer.py``，由 ToolExecutor 呼叫）。

硬地板（``utils/hard_floor.py`` 程式常數）在最前面判斷，設定檔關不掉：F1–F4、F7 三種模式、
F5、F6 在 Auto 下都改成 HUMAN_ONLY（只有人能核准），但不把 DENY 放寬。Auto 下含動態 shell
寫法的指令也是 HUMAN_ONLY。黑名單恆 DENY（凌駕一切、含 YOLO 與 read_only 類）。終端指令
（terminal_execute）一律算 dangerous（舊 safe_whitelist 已拿掉）。read_only 類＝三模式 ALLOW
免 HITL（ADR-0089 語意由 tool_risk 承接）。看不懂的模式值一律 CONFIRM（等同 Manual）。
要隔離的動作（``isolation_policy.requires_isolation``）在 environment 的 ``isolation`` 是 none
（缺鍵、看不懂的值也算）時，Auto 與 YOLO 改成 HUMAN_ONLY（程式常數，設定檔改不鬆）。
YOLO 下 dangerous 的 MCP 工具在 ``human_present`` 不是 True 時也是 HUMAN_ONLY（ADR-0272 決定 4）。
"""

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field
from src.utils.command_safety import has_dynamic_shell_syntax
from src.utils.hard_floor import (
    AUTO_HUMAN_ONLY_CATEGORIES,
    FLOOR_LABELS,
    HUMAN_ONLY_CATEGORIES,
    classify_hard_floor,
)
from src.utils.isolation_policy import normalize_isolation, requires_isolation

from .permissions import BasePolicyModel
from .policy import PolicyContext, PolicyDecision

logger = logging.getLogger(__name__)

RiskClass = Literal["read_only", "safe", "dangerous"]

_RISK_ORDER: tuple[str, ...] = ("read_only", "safe", "dangerous")
# 沒有隔離環境時改成只有人能核准的模式（程式常數，設定檔改不鬆）
_UNISOLATED_HUMAN_ONLY_MODES: frozenset[str] = frozenset({"Auto", "YOLO"})

class SafetyRules(BaseModel):
    """正則表達式基礎的安全規則集（舊檔的 safe_whitelist 鍵讀到會被忽略）"""
    critical_blacklist: list[str] = Field(
        default_factory=list, description="強制攔截的危險指令 Regex 清單"
    )

    def check_match(self, text: str) -> str | None:
        """檢查文字是否命中黑名單，回傳匹配到的第一個 Pattern"""
        if not text:
            return None
        for pattern in self.critical_blacklist:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return pattern
            except Exception:
                logger.exception(f"Invalid regex pattern '{pattern}'")
        return None


class ToolRiskEntry(BaseModel):
    """聚合多動詞工具的分類條目：動詞參數名＋各動詞的風險類。

    廣告（schema_injection）取最低風險——模型能看到聚合工具、能用其唯讀動詞；
    執行取實際動詞的類；動詞缺失／未知＝dangerous（fail-safe，不信任條目自報）。
    """

    action_param: str = Field(
        "action", description="動詞所在的參數名（如 memory_tools 的 action）"
    )
    actions: dict[str, RiskClass] = Field(
        default_factory=dict, description="動詞 → 風險類"
    )


class ToolRiskConfig(BaseModel):
    """工具風險分類 SSOT（HITL 決策表的行輸入）。

    未列名工具（MCP／runtime 動態工具）一律回 ``default``（預設 dangerous，fail-safe）。
    """

    default: RiskClass = Field(
        "dangerous", description="未列名工具（MCP/runtime）的 fail-safe 風險類"
    )
    tools: dict[str, RiskClass | ToolRiskEntry] = Field(
        default_factory=dict, description="工具名 → 風險類（或聚合條目）"
    )

    def class_for(
        self,
        tool_name: str,
        action: str | None = None,
        *,
        advertisement: bool = False,
    ) -> RiskClass:
        """求某次調用（或廣告）的風險類。"""
        entry = self.tools.get(tool_name)
        if entry is None:
            return self.default
        if isinstance(entry, str):
            return entry
        if advertisement:
            # 廣告取最低風險：含唯讀動詞的聚合工具仍可見、可用其唯讀面。
            if not entry.actions:
                return self.default
            return min(entry.actions.values(), key=_RISK_ORDER.index)
        if action is not None and action in entry.actions:
            return entry.actions[action]
        # 動詞缺失／未知 → 一律 dangerous（fail-safe）。
        return "dangerous"

    def action_param_for(self, tool_name: str) -> str | None:
        """聚合工具的動詞參數名；非聚合／未列名 → None（執行層據此抽動詞進審計）。"""
        entry = self.tools.get(tool_name)
        if isinstance(entry, ToolRiskEntry):
            return entry.action_param
        return None


def _require_isolation(context: PolicyContext, decision: PolicyDecision) -> PolicyDecision:
    """不放寬：要隔離的動作在層級 none（缺鍵、看不懂的值也算）時，Auto、YOLO 只有人能核准。

    審查員與 YOLO 都不能放行，無人在場拒絕；Manual 與看不懂的模式維持原判；DENY 不放寬；
    底線已經是 HUMAN_ONLY 時保留它的理由，只多標 ``isolation``。
    """
    environment = context.environment
    mode = environment.get("auth_level", "Manual")
    if (
        mode not in _UNISOLATED_HUMAN_ONLY_MODES
        or decision.effect == "DENY"
        or not requires_isolation(context.action, context.resource)
        or normalize_isolation(environment.get("isolation")) != "none"
    ):
        return decision
    metadata = {**decision.metadata, "isolation": "none"}
    if decision.effect == "HUMAN_ONLY":
        return decision.model_copy(update={"metadata": metadata})
    return PolicyDecision(
        effect="HUMAN_ONLY",
        reason="No isolated execution environment for this action: only a human can approve",
        metadata=metadata,
    )


class AuthPoliciesConfig(BasePolicyModel):
    """全系統安全策略配置 (SSOT)

    對應 auth_policies.json
    """
    policies: SafetyRules = Field(..., description="具體的黑白名單規則")
    tool_risk: ToolRiskConfig = Field(
        default_factory=ToolRiskConfig,
        description="工具風險分類（read_only/safe/dangerous；HITL 決策表輸入）",
    )

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """核心決策引擎：硬地板 → 黑名單 → 風險分類 → 四級決策表 → 隔離層級。"""
        environment = context.environment
        floor = classify_hard_floor(
            context.action,
            context.resource,
            tool_action=environment.get("floor_action") or environment.get("tool_action"),
            is_mcp=bool(environment.get("mcp_server")),
        )
        decision = self._evaluate_rules(context)
        if floor is None:
            return _require_isolation(context, decision)
        metadata = {**decision.metadata, "hard_floor": floor}
        level = environment.get("auth_level", "Manual")
        human_only = floor in HUMAN_ONLY_CATEGORIES or (
            level == "Auto" and floor in AUTO_HUMAN_ONLY_CATEGORIES
        )
        if human_only and decision.effect != "DENY":
            # 只有人能核准；審查員與 YOLO 都不能放行（DENY 不放寬）
            return _require_isolation(
                context,
                PolicyDecision(
                    effect="HUMAN_ONLY",
                    reason=f"Hard floor {floor} ({FLOOR_LABELS[floor]}): only a human can approve",
                    metadata=metadata,
                ),
            )
        return _require_isolation(context, decision.model_copy(update={"metadata": metadata}))

    def _evaluate_rules(self, context: PolicyContext) -> PolicyDecision:
        """設定檔規則：黑名單先行 → 風險分類 → 三模式決策表。"""
        resource = context.resource
        # 獲取模式 (優先從 Environment 讀取，預設為 Manual)
        level = context.environment.get("auth_level", "Manual")

        # --- 第一防禦線：黑名單攔截 (最高優先級，凌駕 YOLO 與 read_only 類) ---
        matched_blacklist = self.policies.check_match(resource)
        if matched_blacklist:
            return PolicyDecision(
                effect="DENY",
                reason=f"Matched critical blacklist pattern: {matched_blacklist}",
            )

        # --- 風險分類（決策表的行）---
        terminal = context.action == "terminal_execute"
        if terminal:
            # 終端：resource 是「指令字串」，一律 dangerous，不吃 read_only 豁免——避免
            # 指令剛好叫 "research" 被誤放行（ADR-0089 回歸鎖）。
            risk: RiskClass = "dangerous"
        else:
            # 工具面：resource 即工具名（tool_call／schema_injection）。聚合工具按
            # environment["tool_action"] 細分；廣告取最低風險；未列名＝default。
            risk = self.tool_risk.class_for(
                resource,
                context.environment.get("tool_action"),
                advertisement=(context.action == "schema_injection"),
            )

        # --- read_only 類：三模式 ALLOW 免 HITL（ADR-0089 語意承接）---
        if risk == "read_only":
            return PolicyDecision(
                effect="ALLOW",
                reason="Read-only tool auto-allowed at all auth levels (ADR-0089)",
            )

        # --- 第二防禦線：三模式 × {safe, dangerous} 決策表 ---
        if level == "Manual":
            # Manual 模式下，所有 safe/dangerous 動作都需人類確認（read_only 已豁免）
            return PolicyDecision(
                effect="CONFIRM", reason="Manual mode requires HITL for all actions"
            )

        if level == "Auto":
            if terminal and has_dynamic_shell_syntax(resource):
                return PolicyDecision(
                    effect="HUMAN_ONLY",
                    reason="Command uses dynamic shell syntax in Auto mode: only a human "
                    "can approve",
                )
            if risk == "safe":
                return PolicyDecision(
                    effect="ALLOW", reason="Safe-class action auto-allowed in Auto mode"
                )
            # 終端與 dangerous 工具（ADR-0246 決定 3）只回「要審」：審查員是非同步呼叫，
            # 由 ToolExecutor 接手（evaluate 維持純函式）
            return PolicyDecision(
                effect="REVIEW", reason="Dangerous action goes to the Auto reviewer"
            )

        if level == "YOLO":
            env = context.environment
            headless = env.get("human_present") is not True
            if env.get("mcp_server") and risk == "dangerous" and headless:
                # ADR-0272 決定 4：無人在場時 dangerous 的 MCP 工具只有人能核准（＝拒絕）
                return PolicyDecision(
                    effect="HUMAN_ONLY",
                    reason="Dangerous MCP tool in YOLO mode with no human present: only a "
                    "human can approve",
                )
            return PolicyDecision(
                effect="ALLOW", reason="YOLO mode permits non-blacklist actions"
            )

        # 預設保險（含已退役的舊名稱）
        return PolicyDecision(
            effect="CONFIRM", reason="Defaulting to confirmation for unknown level"
        )
