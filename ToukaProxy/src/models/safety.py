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
只能看的子代理（environment 的 ``read_only_run``）：不是 read_only 類一律 DENY，
三種模式都一樣（ADR-0309）。
子代理的指令（environment 的 ``subagent_run``）：窄開關沒打開、派它的那一輪已經結束、或這次不在
執行器裡跑，一律 DENY，三種模式都一樣（ADR-0333）；打開時照決策表（Auto 送審、YOLO 放行）。
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
from src.utils.isolation_policy import TERMINAL_TOOLS, normalize_isolation, requires_isolation
from src.utils.presence import PARENT_TURN_ENDED

from .permissions import BasePolicyModel
from .policy import PolicyContext, PolicyDecision

logger = logging.getLogger(__name__)

RiskClass = Literal["read_only", "safe", "dangerous"]

_RISK_ORDER: tuple[str, ...] = ("read_only", "safe", "dangerous")
# 沒有隔離環境時改成只有人能核准的模式（程式常數，設定檔改不鬆）
_UNISOLATED_HUMAN_ONLY_MODES: frozenset[str] = frozenset({"Auto", "YOLO"})
# 執行時的風險至少是這一級，設定檔寫得再鬆也一樣（ADR-0317）：小幫手照 Touka 的模式核准，
# 清單在 Manual 要跳卡、在 Auto 要給審查員。廣告不套，list、read 照樣看得到
MIN_TOOL_RISK: dict[tuple[str, str], RiskClass] = {("local_files", "propose"): "dangerous"}
_raised_once: set[tuple[str, str]] = set()


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

    action_param: str = Field("action", description="動詞所在的參數名（如 memory_tools 的 action）")
    actions: dict[str, RiskClass] = Field(default_factory=dict, description="動詞 → 風險類")


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
        """求某次調用（或廣告）的風險類；執行時套 ``MIN_TOOL_RISK`` 的下限。"""
        risk = self._listed_class(tool_name, action, advertisement=advertisement)
        return risk if advertisement else _at_least_floor(tool_name, action, risk)

    def _listed_class(
        self, tool_name: str, action: str | None, *, advertisement: bool
    ) -> RiskClass:
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
        """聚合工具的動詞參數名；非聚合／未列名 → None（執行層據此抽動詞進審計）。

        內建分類的工具一律用內建的參數名：設定檔寫成一個字串或換了參數名，下限照樣抽得到動詞。
        """
        builtin = BUILTIN_TOOL_RISK.get(tool_name)
        if builtin is not None:
            return builtin.action_param
        entry = self.tools.get(tool_name)
        if isinstance(entry, ToolRiskEntry):
            return entry.action_param
        return None


def _at_least_floor(tool_name: str, action: str | None, risk: RiskClass) -> RiskClass:
    """比下限鬆就改成下限；同一個工具與動作只記一次 warning。"""
    floor = MIN_TOOL_RISK.get((tool_name, action)) if action is not None else None
    if floor is None or _RISK_ORDER.index(risk) >= _RISK_ORDER.index(floor):
        return risk
    if (tool_name, action) not in _raised_once:
        _raised_once.add((tool_name, action))
        logger.warning(
            "Risk for %s %s raised from %s to the built-in floor %s",
            tool_name,
            action,
            risk,
            floor,
        )
    return floor


# 專用空間的專案不能跑（ADR-0332）：任何模式都直接拒絕；拒絕訊息由執行端依這個前綴挑
PROJECT_SPACE_DENY_PREFIX = "project_space_refused:"


def _refused_project(context: PolicyContext, decision: PolicyDecision) -> PolicyDecision | None:
    """``isolation_refused`` 有原因、而且是要隔離的動作：DENY（已經是 DENY 的保留原因）."""
    refused = context.environment.get("isolation_refused")
    if not isinstance(refused, str) or not refused or decision.effect == "DENY":
        return None
    if not requires_isolation(context.action, context.resource):
        return None
    return PolicyDecision(
        effect="DENY",
        reason=f"{PROJECT_SPACE_DENY_PREFIX}{refused}",
        metadata={**decision.metadata, "isolation": "none", "isolation_refused": refused},
    )


# 子代理的指令（ADR-0333）：不符合窄開關就直接拒絕，任何模式都一樣；拒絕訊息由執行端依前綴挑
SUBAGENT_DENY_PREFIX = "subagent_commands:"
SUBAGENT_COMMANDS_NOT_ALLOWED = "not_allowed"  # 派出時不符合（心跳、只能看、Manual、別的專案…）
SUBAGENT_COMMANDS_NEED_EXECUTOR = "needs_executor"  # 這次不在執行器裡跑


def _subagent_command_denial(context: PolicyContext, terminal: bool) -> PolicyDecision | None:
    """子代理呼叫終端：窄開關、派它的那一輪、執行器三者缺一就 DENY（其他情況回 None）.

    工具清單階段（``schema_injection``）resource 是工具名，沒有隔離層級，不看執行器；執行階段
    （``terminal_execute``）resource 是指令。專用空間的專案被拒絕（``isolation_refused``）時
    不在這裡判，交給 ``_require_isolation`` 帶它自己的原因。
    """
    environment = context.environment
    if environment.get("subagent_run") is not True:
        return None
    if not terminal and context.resource not in TERMINAL_TOOLS:
        return None
    if environment.get("subagent_project_commands") is not True:
        reason = SUBAGENT_COMMANDS_NOT_ALLOWED
    elif environment.get("parent_turn_active") is not True:
        reason = PARENT_TURN_ENDED
    elif (
        terminal
        and not environment.get("isolation_refused")
        and normalize_isolation(environment.get("isolation")) != "executor"
    ):
        reason = SUBAGENT_COMMANDS_NEED_EXECUTOR
    else:
        return None
    return PolicyDecision(effect="DENY", reason=f"{SUBAGENT_DENY_PREFIX}{reason}")


def _require_isolation(context: PolicyContext, decision: PolicyDecision) -> PolicyDecision:
    """不放寬：要隔離的動作在層級 none（缺鍵、看不懂的值也算）時，Auto、YOLO 只有人能核准。

    審查員與 YOLO 都不能放行，無人在場拒絕；Manual 與看不懂的模式維持原判；DENY 不放寬；
    底線已經是 HUMAN_ONLY 時保留它的理由，只多標 ``isolation``。專用空間的專案被拒絕時
    （ADR-0332）任何模式都是 DENY（``_refused_project``）。
    """
    refused = _refused_project(context, decision)
    if refused is not None:
        return refused
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


# 舊的設定檔沒有列到的工具仍是 dangerous（fail-safe）。這個工具自己的核准在使用者電腦上的
# 小幫手視窗（ADR-0312），只有設定檔成功載入、又沒列到它時才併進去；讀不到設定檔不適用。
BUILTIN_TOOL_RISK: dict[str, ToolRiskEntry] = {
    "local_files": ToolRiskEntry(
        action_param="action",
        actions={
            "list": "read_only",
            "read": "read_only",
            "propose": "dangerous",
            "create_folder": "safe",
            "create_file": "safe",
        },
    ),
}


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

        # --- 子代理的指令：窄開關以外一律拒絕，YOLO 也放不寬（ADR-0333）---
        subagent_denial = _subagent_command_denial(context, terminal)
        if subagent_denial is not None:
            return subagent_denial

        # --- 只能看的子代理：排在模式決策表之前，YOLO 也放不寬（ADR-0309）---
        if context.environment.get("read_only_run") and risk != "read_only":
            return PolicyDecision(
                effect="DENY", reason="This sub-agent was dispatched as read-only"
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
            return PolicyDecision(effect="ALLOW", reason="YOLO mode permits non-blacklist actions")

        # 預設保險（含已退役的舊名稱）
        return PolicyDecision(
            effect="CONFIRM", reason="Defaulting to confirmation for unknown level"
        )


def with_builtin_tool_risk(config: AuthPoliciesConfig) -> AuthPoliciesConfig:
    """把設定檔沒列到的內建分類補進去；設定檔有列時以設定檔為準。"""
    tools = config.tool_risk.tools
    missing = {name: entry for name, entry in BUILTIN_TOOL_RISK.items() if name not in tools}
    if not missing:
        return config
    risk = config.tool_risk.model_copy(update={"tools": {**tools, **missing}})
    return config.model_copy(update={"tool_risk": risk})
