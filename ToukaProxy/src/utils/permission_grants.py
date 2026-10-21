"""權限授權值物件 SSOT（permission-representation-convergence P1/P2）。

把 ``"*"`` 從 ``list[str]`` 裡的哨兵值升成獨立欄位。

**問題**：``"*"`` 塞在清單裡時**必然**同時是兩種東西——allow 面的「量詞」（全部）與
deny 面的「元素」（一個字面上叫做 ``*`` 的工具名）。這不是美觀問題：現況
`permission_service` 的 allow 面把它當量詞、deny 面當名字，而系統**正是靠這個不一致
才活著**（`Base` 的 ``denied_tools: ["*"]`` 沿繼承鏈聯集到所有角色，若 deny 面也當量詞
則全系統工具立刻歸零）。

**解法**：`open_ended`（開放式授權，含現在與未來）與 `deny_all`（真正的全關）是兩個
獨立布林，`explicit`／`denied` 只裝真實名稱。於是：

- 詞性歧義消失——沒有任何欄位同時是量詞和元素。
- 合併不需要特例塌縮（布林用 ``or``、集合用 ``|``），`merge_with` 那三行
  ``if "*" in x: x = ["*"]`` 隨之退場。
- 兩個閘門呼叫**同一個** `allows()`，「對同一份設定給出不同答案」變成結構上不可能。

**為什麼保留開放式授權而不改成全列舉**（已評估並否決的替代案）：
MCP 工具由 hub 動態提供、自我演化建的技能在執行期才誕生——列舉在定義上無法涵蓋
「尚未存在的東西」。該禁的是把 ``"*"`` 當成「集合的縮寫」或「未設定的預設」，不是
開放式授權本身。

本模組純值物件：不讀檔、不碰身分、無 IO、無時間依賴。P2 起由
``PermissionService.resolve_grants`` 作為唯一解析器接線。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from src.utils.mcp_tool_policy import WILDCARD, matches_tool_ref, server_of_ref

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class _GrantBase:
    """一個權限軸的完整授權狀態。

    四個欄位互相正交，任兩者的組合都有明確語意：

    ==============  ==================================================
    欄位            語意
    ==============  ==================================================
    ``open_ended``  開放式授權：**含現在與未來**的全部（原 allow 面 ``"*"``）
    ``explicit``    明示授予的名稱（不含 ``"*"``）
    ``deny_all``    真正的全關（原 deny 面 ``"*"``——今日是死語意，見 `from_lists`）
    ``denied``      明示拒絕的名稱（不含 ``"*"``）
    ==============  ==================================================

    拒絕恆優先：``deny_all`` 或命中 ``denied`` 即拒絕，``open_ended`` 也擋不住。
    """

    open_ended: bool = False
    explicit: frozenset[str] = frozenset()
    deny_all: bool = False
    denied: frozenset[str] = frozenset()

    @classmethod
    def from_lists(
        cls, allowed: list[str] | None, denied: list[str] | None = None
    ) -> Self:
        """從現行 ``list[str]`` 設定形狀解析（選項 α：JSON／wire 形狀不變）。

        ⚠️ **``denied`` 裡的 ``"*"`` 在此被解讀為「全關」，這是行為變更。**
        現況 `permission_service` 把它當字面工具名（永不匹配任何真實工具），因此
        `Base` 的 ``denied_tools: ["*"]`` 今天是個無聲的 no-op。

        這個變更**只有在拒絕面不沿繼承鏈傳播時才是安全的**（見 `extend`）——否則
        `Base` 那筆 ``"*"`` 會被 `Guest`／`Owner`／`Debugger` 一路繼承下去，把全系統
        關成空。兩者必須同批落地，拆開任一半都會炸。
        """
        allowed = allowed or []
        denied = denied or []
        return cls(
            open_ended=WILDCARD in allowed,
            explicit=frozenset(allowed) - {WILDCARD},
            deny_all=WILDCARD in denied,
            denied=frozenset(denied) - {WILDCARD},
        )

    def _allows(self, matches: Callable[[str], bool]) -> bool:
        """共用判別算法；``matches(pattern) -> bool`` 由子類決定如何比對目標。"""
        if self.deny_all or any(matches(p) for p in self.denied):
            return False
        return self.open_ended or any(matches(p) for p in self.explicit)

    @property
    def is_empty(self) -> bool:
        """是否完全沒有授予或拒絕規則。"""
        return not (self.open_ended or self.explicit or self.deny_all or self.denied)

    def extend(self, parent: Self) -> Self:
        """角色繼承：**授予面向上累加、拒絕面不繼承**。

        ⚠️ 這是對現況 `RoleConfig.merge_with` 的**刻意語意修正**。現況對 deny 取聯集
        （子角色繼承父角色的拒絕），在 Touka 的「能力鏈」語意下方向是反的——
        `Owner` inherits `Guest` 意味著 Owner ⊇ Guest，但 deny 聯集使 Owner 在拒絕軸上
        永遠 ⊆ Guest，於是更高權限的角色**永遠無法解除**較低角色的限制。

        實際後果：Owner 永久帶著 Guest 的 ``edit_soul`` 拒絕，而那是 ADR-0119/0120 刻意
        建立的原生工具。今天沒出事只因為唯一會扣除它的閘門（`is_authorized`）零呼叫端。

        要表達「全系統禁令」請改用全域地板（見 `with_floor`），語意明確且不依賴繼承拓樸。
        """
        return type(self)(
            open_ended=self.open_ended or parent.open_ended,
            explicit=self.explicit | parent.explicit,
            deny_all=self.deny_all,
            denied=self.denied,
        )

    def with_floor(self, floor: Self) -> Self:
        """套上全域禁令地板：對所有角色生效，**含 Owner**。

        只讀 ``floor`` 的拒絕面——**地板永遠不能授予任何東西**。這是刻意的不對稱：
        一個能放寬的「地板」等同第二套 RBAC，而權限系統只該有一個授予來源。
        """
        return type(self)(
            open_ended=self.open_ended,
            explicit=self.explicit,
            deny_all=self.deny_all or floor.deny_all,
            denied=self.denied | floor.denied,
        )


@dataclass(frozen=True)
class NameGrant(_GrantBase):
    """精確名稱比對的授權軸——用於**工具**（`allowed_tools`）與**技能**（`allowed_skills`）。

    刻意不為技能另立一個行為相同的型別：兩軸的比對規則逐字元相同，多一個沒有行為差異的
    名字只會讓人以為它們哪裡不一樣。若日後技能軸真的需要樣式比對，屆時再分。

    同樣刻意**不支援** ``create_*`` 這類前綴 glob（與 `mcp_tool_policy._matches_name`
    一致）：勾選面是一份明確清單，模糊比對會讓上游新增的工具靜默落進既有樣式。
    """

    def allows(self, name: str) -> bool:
        """這個名稱是否獲准。"""
        return self._allows(lambda pattern: pattern == name)


@dataclass(frozen=True)
class McpGrant(_GrantBase):
    """``server`` / ``server/tool`` 樣式比對的授權軸——用於 MCP 兩軸（ADR-0195 軸 B）。

    比對規則**委派** `mcp_tool_policy.matches_tool_ref`，不在此重刻——那是 ADR-0195 已
    落地的 SSOT，同時被 Guard #32 的形狀驗證共用。重刻一份就是讓兩者必然漂移。
    """

    def allows(self, server_name: str, tool_name: str) -> bool:
        """這台伺服器的這個工具是否獲准。"""
        return self._allows(
            lambda pattern: matches_tool_ref(pattern, server_name, tool_name)
        )

    def allows_server(self, server_name: str) -> bool:
        """是否獲准**掛載**這台（工具面收斂交由 `allows`）。

        與 `is_mcp_server_allowed` 的既有語意對齊：角色只被授予該台的個別工具
        （``stripe/create_refund``）時仍須允許掛載，否則連線都建不起來、工具面授權
        形同虛設。故此處比對條目的**伺服器部位**，不看工具部位。

        ⚠️ **刻意完全不讀拒絕面**（連 ``deny_all`` 也不讀）——與現況 `is_mcp_server_allowed`
        逐位元一致。掛載不是權限邊界，`allows` 才是：一台伺服器即使所有工具都被拒，
        掛上去也拿不到任何東西，而「拒個別工具卻連帶擋掉掛載」會讓同台的其他授權工具
        一起失效。真正的邊界留在工具面單點執法。
        """
        if self.open_ended:
            return True
        return any(server_of_ref(pattern) == server_name for pattern in self.explicit)


@dataclass(frozen=True)
class RoleGrants:
    """一個角色在三個權限軸上的完整解析結果。

    ``PermissionService`` 的所有廣告面與執行面入口都只消費此物件，避免各入口自行解讀
    ``RoleConfig`` 或 ``"*"``。技能目前沒有設定檔 deny 軸，因此只使用 ``NameGrant``
    的授予面；工具與 MCP 則同時套用角色自身拒絕與全域地板。
    """

    tools: NameGrant = field(default_factory=NameGrant)
    mcp: McpGrant = field(default_factory=McpGrant)
    skills: NameGrant = field(default_factory=NameGrant)

    def extend(self, parent: RoleGrants) -> RoleGrants:
        """繼承父角色授予面；各軸拒絕面維持 child self-only。"""
        return RoleGrants(
            tools=self.tools.extend(parent.tools),
            mcp=self.mcp.extend(parent.mcp),
            skills=self.skills.extend(parent.skills),
        )

    def with_floor(self, *, tools: NameGrant, mcp: McpGrant) -> RoleGrants:
        """套用全系統拒絕地板；技能軸目前刻意沒有 deny 語意。"""
        return RoleGrants(
            tools=self.tools.with_floor(tools),
            mcp=self.mcp.with_floor(mcp),
            skills=self.skills,
        )
