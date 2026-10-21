# 提案：權限表示法收斂（Permission Representation Convergence）

> **狀態：✅ 已完成（2026-07-27；P0–P6 全數落地，決策收錄於 ADR-0196）。**
> 進度：**P0 ✅ 基線凍結**（§5.2）／**P1 ✅ `Grant` 值物件**（§5.3）／
> **P2 ✅ 解析器接線**（§5.4）／**P3 ✅ UI 解除單向門**（§5.5）／
> **P4 ✅ fallback 收斂**（§5.6）／**P5 ✅ fail-open 本體**（§5.7）／
> **P6 ✅ 守衛與文件**（§5.8）。
> 前置討論見 [ADR-0195](../adr/0195-mcp-per-tool-governance-two-axis.md) §「已知限制」。
> 本提案吸收原本獨立列管的 `_load_config` fail-open 項——兩者同源，分開修會讓前者
> 建立在還會被改掉的表示法上。

---

## 1. 一句話總結

`"*"` 是一個塞在 `list[str]` 裡的哨兵值，因此它**必然**同時是「量詞」（allow 面＝全部）
和「元素」（deny 面＝一個叫做 `*` 的工具名）——本提案把它升到型別層成為獨立的
`open_ended: bool`，讓四個權限軸共用一個 `Grant` 值物件與**唯一一個解析器**，
使「兩個閘門對同一份設定給出不同答案」從「靠紀律防」變成「結構上不可能」。

## 2. 問題陳述（全部經核碼＋執行期實測，2026-07-26）

### 2.1 實測基線

在 3.12 venv 直接呼叫生產 `permission_service`：

```
Base      allow=[]        deny=['*']                                mcp=[]
Guest     allow=13 項      deny=['*','edit_soul','update_my_soul']   mcp=[]
Owner     allow=['*']     deny=['*','edit_soul','update_my_soul']   mcp=['*']
Debugger  allow=['*']     deny=['*','edit_soul','update_my_soul']   mcp=['*']

Gate1  get_allowed_tools(Owner)         = ['*']    ← 廣告 edit_soul
Gate2  is_authorized(Owner,'edit_soul') = False    ← 拒絕 edit_soul
Gate1  len(get_allowed_tools(Guest))    = 13       ← 繼承來的 deny=['*'] 沒殺掉任何東西
```

### 2.2 五條矛盾

| 編號 | 矛盾 | 證據 |
|---|---|---|
| **M1** | 同一個 `"*"` 有兩種詞性：allow 面是量詞、deny 面是**字面工具名** | `permission_service.py:398` vs `:401`／`:360` |
| **M2** | 兩個閘門對 Owner 給出相反答案，而活下來的那個純屬偶然 | `get_allowed_tools:398` 短路不扣 denied；`is_authorized:359` 扣。後者**全 repo 零生產呼叫端** |
| **M3** | `denied_tools` 事實上只是「不廣告」，不是「拒絕」 | 全 repo 僅兩個消費點（`:360` 未接線、`:401` 廣告面且對 Owner 短路）；執行面 `tool_executor` 無獨立把關 |
| **M4** | `merge_with` 需要三行特例塌縮 | `permissions.py:65-67`，唯一理由是 `"*"` 得在 list 裡保持唯一 |
| **M5** | UI 面 `"*"` 是單向門 | `RbacMatrix.tsx:78`：清單含 `*` 時所有勾選框 `disabled`，勾選面無解除路徑 |

**M1 不是美觀問題——系統是靠這個不一致才活著的。** Base 寫 `denied_tools: ["*"]`，
`merge_with` 沿繼承鏈聯集上去，Guest／Owner／Debugger 全都帶著它。若有人「修正」
deny 面讓 `*` 也當量詞，**全系統所有角色的所有工具立刻歸零**。

**M2 的具體後果**：產品上正確的行為（Owner 拿得到 `edit_soul`，ADR-0119/0120 刻意
建立的原生工具）取決於一段死碼繼續維持死著。任何人「順手把 Gate 2 接上」都會是
一次無預警的功能移除。

### 2.3 繼承方向錯誤（M2 的結構根因）

`_get_effective_permissions` 的合併是 `child.merge_with(parent)`，而
`merge_with` 對 deny 面取**聯集**——即**子角色繼承父角色的拒絕**。

allow 面這樣是對的（Owner extends Guest extends Base 逐層累加能力）。deny 面反了：
在「能力鏈」語意下，更高權限的子角色**永遠不可能**解除父角色的限制。Owner 因此
永久帶著 Guest 的 `edit_soul` 拒絕。

現況設定裡兩筆 deny 其實**都是冗餘的**（allow 面本來就沒給），所以今天無害；
但它們正是「統一解析器一上線就靜默扣掉 Owner 兩個工具」的地雷。

### 2.4 fail-open 的實際形狀（原獨立列管項）

`_load_config:218` 對**任何**例外一律 `roles_config = DEFAULT_PERMISSIONS_CONFIG`。
實測該 fallback 與 live 設定的差異：

| | live | fallback | 方向 |
|---|---|---|---|
| Base `allowed_mcp_servers` | `[]` | `["*"]` | **越權放大** |
| 全體 `denied_tools` | Guest 兩筆 | 全空 | 放寬 |
| Base／Guest `allowed_tools` | 0／13 項 | `query_memory`、`search_vault` | 收緊（見下） |

```
fallback 下 Base 能掛任意 MCP 伺服器? True    （live: False）
```

而且——**`DEFAULT_PERMISSIONS_CONFIG` 裡的 `query_memory` 與 `search_vault`
兩個名稱都不在 `tool_registry`（40 個真實工具）中。** 這正是 Guard #32 當初為了
消滅而誕生的「死字串」類型，但守衛只掃 `config/permissions.json` 與 `.example`，
**掃不到這個寫在 Python 裡的常量**。

所以 fallback 不是一個連貫的「安全模式」，是一份帶死字串的陳舊快照：某些軸更寬、
某些軸更窄、無人維護、無守衛涵蓋。「fail-open」低估了它——正確描述是 **fail-unpredictable**。

### 2.5 這份 fallback 為什麼能腐爛這麼久（P0 執行時發現）

`tests/conftest.py` 的 session 沙盒**只播種 `auth_policies.json`，不播種
`permissions.json`**。因此 `permission_service.config_path` 在測試中恆為不存在的路徑，
`_load_config:206-208` 每次都落到 `DEFAULT_PERMISSIONS_CONFIG`。

實測（P0 探針）：

```
config_path = <sandbox>/config/permissions.json
exists      = False
is fallback = True
```

也就是說——**整個測試套件跑的是 fallback，生產 `permissions.json` 的行為幾乎沒有
測試涵蓋**（例外：少數自帶 tmp config 的檔案，如 `test_rbac_hierarchy.py`、
`test_rbac_mcp_tool_inheritance.py`，但它們用的是**合成**設定而非生產內容）。

兩個後果：

1. 死字串能存活，是因為它們正是測試唯一看得到的東西，而沒有任何測試斷言它們是真工具。
2. **M2 在測試中不可見**——fallback 的 `denied_tools` 全空，兩個閘門「碰巧」一致。
   既有測試套件永遠不會踩到這條矛盾。

P0 的矩陣因此必須同時凍結**兩份**設定；只凍結其一都會讓遷移在另一邊無聲漂移。

## 3. 已評估並否決的替代案：全面禁用 `"*"`

擁有者提出的方案：禁用萬用字符，allow／deny 一律詳列。評估結論：**不採用**。

| | 全面禁用 `*` 修得掉？ |
|---|---|
| M1 兩種詞性 | ⭕ 符號沒了，矛盾自然沒了 |
| **M2 兩閘門分歧** | **❌ 完全無效** |
| M3 deny 只是不廣告 | ❌ 無關 |
| M4 塌縮特例 | ⭕ |
| M5 UI 單向門 | ⭕ |

M2 修不掉的理由是決定性的：矛盾來自**兩份獨立實作**而非符號。把 `["*"]` 換成 40 個
名字，`if role_key == "Owner": return ALL_TOOLS` 照樣寫得出來，兩個閘門照樣可以
一個扣 denied、一個不扣。而 M2 是五條裡唯一一條會讓「設定寫的」與「實際發生的」
不一致的。

**另有兩處會變得更糟：**

1. **MCP 工具集在設定時無法列舉。** 這是 Guard #32「驗不了存在性」邊界的正面：
   要列舉就得先連上伺服器才能寫設定——宣告式設定、`.example`、git 追蹤的 config
   全部破功。替代方案「同步工具清單按鈕」＝帶陳舊 bug 的萬用字符。
2. **自我演化建的技能會鎖死自己。** Owner 的 `allowed_skills=["*"]`；agent 自建技能
   後需立即可用。改成列表即「建完不能用，等人改 permissions.json」，直接斷掉
   ADR-0148／0149 那條線。

**次要考量**：漏列可用 parity 棘輪擋（如 `tool_risk` 已在做），但那是每新增一個工具
就要在 4 角色 × 2 檔＝8 個位置做決定，其中 7 次幾乎恆等於該工具的風險類。棘輪的
價值與它每次索取的決策數成反比——`tool_risk` 是 1 決策／工具所以有效；per-role
allowlist 是 8 決策／工具，會退化成「把 CI 弄綠的最省事寫法」＝全部加進去。

**結論**：`"*"` 承載「開放式授權」（含未來工具、含尚未連線的 MCP、含還沒建出來的
技能）是正當且不可替代的——列舉在定義上做不到這件事。該禁的是它被當成「集合的
縮寫」或「未設定的預設」。ADR-0195 在 `denied_mcp_tools` 禁裸 `*` 就是這個原則的
外科手術版，本提案是它的一般化。

## 4. 目標模型

### 4.1 `Grant` 值物件

```python
class Grant(BaseModel, frozen=True):
    """一個權限軸的完整授權狀態。`open_ended` 是原本的 allow-`*`，升為獨立欄位。"""
    open_ended: bool = False          # 含現在與未來的全部（原 allow "*"）
    explicit: frozenset[str] = frozenset()
    deny_all: bool = False            # 真正的全關（原 deny "*"，今日為死語意）
    denied: frozenset[str] = frozenset()

    @staticmethod
    def _matches(pattern: str, target) -> bool:
        raise NotImplementedError

    def allows(self, target) -> bool:
        if self.deny_all or any(self._matches(p, target) for p in self.denied):
            return False
        return self.open_ended or any(self._matches(p, target) for p in self.explicit)

    def extend(self, parent: Grant) -> Grant:
        """繼承：allow 面向上累加；deny 面**不繼承**（見 §4.2）。"""
        return type(self)(
            open_ended=self.open_ended or parent.open_ended,
            explicit=self.explicit | parent.explicit,
            deny_all=self.deny_all,
            denied=self.denied,
        )
```

三個具體軸：

| 型別 | `_matches` | 用於 |
|---|---|---|
| `ToolGrant` | `pattern == target`（精確） | `allowed_tools`／`denied_tools` |
| `SkillGrant` | `pattern == target` | `allowed_skills`（今日無 deny 軸） |
| `McpGrant` | `mcp_tool_policy.matches_tool_ref(pattern, *target)` | `allowed_mcp_servers`／`denied_mcp_tools` |

`McpGrant` 直接複用 ADR-0195 已落地的 SSOT，零新比對規則。

### 4.2 deny 語意的兩項變更（需擁有者拍板）

**(a) deny 自身生效、不沿繼承鏈往子角色傳播。**
理由見 §2.3。效果：Owner 不再繼承 Guest 的 `edit_soul` 拒絕 → **零行為變更**
（今日 Owner 實際上就是拿得到 `edit_soul`）；Guest 自己的拒絕仍然生效。

**(b) 新增全域地板 `PermissionsConfig.global_denied_*`，對所有角色（含 Owner）生效。**
這是「把 deny 放在根角色讓它傳播下去」這個今日慣用法的**顯式**替代品。缺了它，
(a) 會拿掉唯一一種表達「全系統禁令」的方式。語意上等同 ADR-0195 的軸 A（供給面
自我約束，連 Owner 都綁得住）。

> **拍板點 1｜✅ 已採納（2026-07-26）**：(a)+(b) 皆執行。
> （若當時否決 (a)，統一解析器上線時 Owner 會失去 `edit_soul` 與 `update_my_soul`
> 的廣告面——那會是一次刻意的功能移除。實際影響的 cell 見 §5.1。）

### 4.3 兩個閘門合流

```python
def resolve_grants(role_key: str | None) -> RoleGrants   # 唯一解析器
```

- `get_allowed_tools(identity) -> list[str]` **回傳型別本身就是矛盾的載體**：一個
  「可能無限」的集合被迫塞進 list，因此只能用哨兵。改為回傳 `ToolGrant`。
- `is_tool_allowed(name, allowed_tools)` → `grant.allows(name)`。
- `is_authorized` 改為 `resolve_grants(...).tools.allows(name)`——與 Gate 1 同一個
  物件、同一個方法。**M2 結構上不可能再發生。**
- `role_key == "Owner"` 硬編（`:359`／`:398`／`:420`／`:463` 四處）刪除：Owner 的
  `open_ended=True` 是**資料**。順帶消掉一個今日的潛在陷阱——那個硬編對現行設定是
  死碼（Owner 本來就有 `["*"]`），但只要有人手改 permissions.json 把 Owner 改窄，
  它會**無聲把權限還原**，設定檔說謊。

### 4.4 角色設定 wire 形狀：**不變**（選項 α）

JSON 仍寫 `allowed_tools: ["*"]`，只在**載入時**解析成 `Grant`。P2 為了 §4.2(b)
新增兩個**可選、預設空陣列**的頂層欄位 `global_denied_tools` 與
`global_denied_mcp_tools`；這是 additive schema 變更，不把既有角色欄位改成 `Grant`
物件，也不要求遷移既有設定檔。

理由：M1／M2／M4 的真正危害都在**解析端**；檔案層的 `"*"` 只要有唯一解析器，就不
再有兩種詞性。選項 β（JSON 也改成 `{"open_ended": true, "explicit": [...]}`）會帶來
破壞性 wire 變更、`openapi.json`＋`api.ts` 重生、`RbacMatrix` 重寫、既有使用者設定檔
遷移——換到的只是「檔案長得比較好看」。

> **拍板點 2｜✅ 已採納（2026-07-26）**：先做 α，既有角色欄位的
> wire／JSON 形狀不變。
> β 保留為日後獨立提案。

M5（UI 單向門）不需要 β 就能修：`RbacMatrix` 改成「開放式授權」toggle ＋ 勾選清單
兩層，關掉 toggle 時送 `allowed_tools: []`、開啟時送 `["*"]`。與 ADR-0195 的
`enabled_tools` UI（送 `["*"]`／`[]`）同一套慣例。

## 5. 遷移路徑（絞殺者，每階段全綠可交付）

| 階段 | 內容 | 驗收 |
|---|---|---|
| **P0 基線凍結** ✅ | 把「每個角色 × 每個工具／技能／MCP 的有效答案」寫成 parametrized 矩陣測試（比照 ADR-0141 P0 的 221 條凍結）。後續每階段都必須通過同一張表 | 矩陣測試綠；刻意變更需在表上逐項標記 |
| **P1 `Grant` 型別** ✅ | 純值物件＋子型別＋單測，**不接線**。`McpGrant` 複用 `mcp_tool_policy` | 純函式測試綠；變異驗證 `allows` 的四個分支 |
| **P2 解析器接線** ✅ | `resolve_grants` 上線；兩個閘門改呼叫它；刪四處 Owner 硬編；deny 改 self-only＋加 `global_denied_*` | Gate 1／技能／MCP 三軸**零變動**；Gate 2 恰 4 個 cell 由 False→True（見 §5.1） |
| **P3 UI 解除單向門** ✅ | `RbacMatrix` 開放式授權 toggle（M5） | vitest；Owner 可由 `*` 改成清單再改回來 |
| **P4 fallback 收斂** ✅ | 修 `DEFAULT_PERMISSIONS_CONFIG` 兩個死字串；把它移到零相依模組讓 Guard #32 能 import 並納入掃描；fallback 改為「僅 Owner 開放式、其餘全封閉」 | Guard #32 抓得到植入的死字串（變異驗證） |
| **P5 fail-open 本體** ✅ | `_load_config` 拆兩段語意：**啟動期**檔案存在且非空但解析失敗 → **拒絕啟動**（fail-fast）；**熱重載期**解析失敗 → **保留舊設定**、不降級、記 audit＋健康端點標記降級 | 三條路徑皆有測試；bootstrap（檔案不存在／空白／`{}`）仍走 fallback |
| **P6 守衛與文件** ✅ | ADR＋`SECURITY.md`／`security_auth_guide.md` 同步；`ROLE_MODEL.md` 補權限表示法節 | `guard_adr_integrity` 綠；ADR 反向註記 |

**P5 是原本獨立列管的 fail-open 項**——放在最後是因為它的正確修法依賴 P2 之後的
表示法（「拒絕恆優先且不可被預設抹掉」在型別層有保證後，降級的爆炸半徑才可控）。

### 5.1 P2 將刻意變更的 4 個 cell（修正本文初稿的「零變動」）

初稿在 P2 寫「P0 矩陣零變動」，**那是錯的**。P0 的真值表跑出來後可精確推導：

生產設定的 Owner／Debugger 自身 `denied_tools` 為空（那兩筆 deny 屬於 Guest）。
deny 改 self-only 後，統一解析器對 Owner 的 `edit_soul`／`update_my_soul` 回 **True**
——與 Gate 1 現況一致、與 Gate 2 現況相反。故：

| cell | 現況 | P2 後 |
|---|---|---|
| Gate 2（prod, Owner, `edit_soul`／`update_my_soul`） | False | **True** |
| Gate 2（prod, Debugger, 同上） | False | **True** |
| Gate 1／技能／MCP 全部 cell | — | 不變 |

**生產行為零變更**——Gate 2 全 repo 零呼叫端，這 4 個 cell 今天不影響任何執行路徑。
但矩陣會變，因此必須以顯式 diff 呈現，不能宣稱「零變動」。

收斂方向是**往已接線的那一邊**（Gate 1）。反向收斂（讓 Gate 1 也扣除繼承來的 deny）
會讓 Owner 失去 `edit_soul`／`update_my_soul` 的廣告面——那是 ADR-0119/0120 刻意
建立的原生工具，等同一次無預警的功能移除。

### 5.2 P0 產出（2026-07-26 已落地）

`tests/unit/test_permission_matrix_baseline.py`，74 個 case：

- 工具軸：2 設定 × 5 角色 × 兩個閘門，各自對**全部 40 個真實工具**做集合相等斷言
  （工具名取自執行期 `tool_registry` 本體，不重刻清單）。
- `EXPECTED_DIVERGENCE`：兩閘門分歧的 cell **獨立硬編**，刻意不寫成
  `GATE1 ^ GATE2`——後者可由前兩張表推導，於是「兩張表被一致地改掉」時會跟著變綠。
- 技能軸探針含一個**尚未存在的技能 id**：開放式授權不可被列舉取代的直接證據。
- MCP 兩軸；`Base` 在兩份設定下的相反答案由一條獨立測試對**兩個真實服務**取值
  （初稿寫成讀硬編表，是同義反覆、對生產變更零判別力，已修）。
- 前提事實：registry 40 項、`permissions.json` ≡ `.example`、fallback 死字串集合
  （P4 修好後該條必須反轉為空集合）。

變異驗證 3/3 全紅，命中 cell 與預測一致：

| 變異 | 紅的 case |
|---|---|
| Gate 2 停止對 Owner 扣除 denied | `gate2[prod-Owner]`、`gate2[prod-Debugger]`、`divergence[prod-Owner]`、`divergence[prod-Debugger]` |
| fallback 的 Base 不再 `["*"]` MCP | `mcp_matrix[fallback-Base-×2]`、`fallback_is_more_permissive` |
| 生產設定給 Guest 多一個工具 | `gate1[prod-Guest]`、`gate2[prod-Guest]`、`production_config_equals_example` |

### 5.3 P1 產出（2026-07-26 已落地）

`ToukaProxy/src/utils/permission_grants.py` ＋ `tests/unit/test_permission_grants.py`
（31 個 case）。**全 repo 零生產呼叫端**（已 grep 確認），接線在 P2。

**與 §4.1 的兩處偏離（刻意）**：

1. **子型別是 2 個不是 3 個。** 工具軸與技能軸的比對規則逐字元相同，故合為
   `NameGrant`；MCP 軸為 `McpGrant`。多立一個沒有行為差異的 `SkillGrant` 只會讓人
   以為它們哪裡不一樣。日後技能軸真需要樣式比對再分。
2. **用 frozen dataclass 而非 `BaseModel`。** Grant 不從磁碟或 wire 來（選項 α 下
   `RoleConfig` 仍是唯一的 Pydantic 邊界），用 BaseModel 會誤導成它是個 wire 模型，
   且每次解析都付驗證成本。禁令 17 的約束落在 `RoleConfig`，不在此。

**新增的第三個組合子 `McpGrant.allows_server`**（§4.1 草圖沒有，但 P2 需要）：
答「掛不掛這台」。它**刻意完全不讀拒絕面，連 `deny_all` 也不讀**——與現況
`is_mcp_server_allowed` 逐位元一致。理由：掛載不是權限邊界，`allows` 才是；
「拒個別工具卻連帶擋掉掛載」會讓同台其他授權工具一起失效。

**寫作過程中自我修正一處**：`allows_server` 初版重刻了「取條目的伺服器部位」邏輯
（`pattern.startswith(f"{server}/")`），正是本提案反覆警告的 SSOT 漂移。已改為委派
`mcp_tool_policy.server_of_ref`。

變異驗證 6/6 全紅：

| 變異 | 紅的 case |
|---|---|
| `_allows` 不看 `deny_all` | `test_deny_all_beats_everything` |
| `_allows` 不看 `denied` | `test_extend_keeps_own_denials` |
| `_allows` 忽略 `open_ended` | `test_open_ended_allows_unknown_future_names` |
| `_allows` 忽略 `explicit` | `test_deny_all_does_not_propagate_to_child` |
| `extend` 恢復拒絕面聯集（＝現況 `merge_with` 語意） | `test_deny_all_does_not_propagate_to_child` |
| `with_floor` 讓地板也能放寬 | `test_floor_can_never_widen` |

> **Guard #20 的已知盲點覆核**：本模組零呼叫端仍讓 Dead Code Guard 通過——vulture 對
> 未使用類別／方法的 confidence 為 60，低於門檻 80。這與先前記錄的限制一致：
> **守衛抓不到「新增但未接線」的程式碼**，故 P2 的接線必須靠行為測試證明，不能靠守衛。

### 5.4 P2 產出（2026-07-26 已落地）

- `PermissionService.resolve_grants()` 成為角色繼承、角色本地 deny、全域 deny 地板的
  唯一解析器；Gate 1、Gate 2、技能與 MCP 全部委派同一份 `RoleGrants`。
- `get_allowed_tools()` 改回傳 `NameGrant`，呼叫端只透過 `allows()`／`is_empty`
  消費；四處 `role_key == "Owner"` 特判與 `RoleConfig.merge_with()` 已刪除。
- deny 改為 self-only；`global_denied_tools`／`global_denied_mcp_tools` 是唯一跨角色
  拒絕地板，Owner 亦受約束。角色 deny 的裸 `"*"` 現在安全地表示該角色 deny-all。
- P0 真值表只翻轉 §5.1 指定的 4 個 Gate 2 cell，兩閘門分歧集合歸零；主要 P2 回歸組
  210 tests 全綠。
- Guard #32 同步接受角色本地 deny-all，並驗證兩個全域地板的工具名／MCP 條目形狀。
- 新增頂層欄位使 OpenAPI 發生 additive 變更，已重生 `openapi.json` 與前端
  `src/types/api.ts`；這修正了本文初稿把「選項 α」誤寫成「整份 OpenAPI 零變更」的矛盾。
- 最終驗證：pytest **4019 passed / 5 skipped**、vitest **1122 passed / 219 files**、
  guard_all **43/44**（僅既知 Guard #23 npm audit 紅）。

### 5.5 P3 產出（2026-07-26 已落地）

- `RbacMatrix` 的原生工具、MCP 伺服器與技能三軸共用 `OpenEndedGrantList`，每軸皆有
  「開放式授權」toggle；開啟時 wire 值為 `["*"]`，關閉時為 `[]`，明確清單只在
  關閉時可編輯。
- Owner 的自動化互動路徑已釘死：`["*"] → [] → ["read_file"] → ["*"]`，M5 的單向門
  消失；MCP 與技能另測只改自己的欄位，不互相踩踏。
- 原本以淺拷貝後直接修改 `permissions.roles[role]` 的更新方式，同步收斂成
  `withRoleList()` 不可變更新；包括既有 `denied_mcp_tools` 編輯，不再污染傳入物件。
- 新增 `RbacMatrix.test.tsx` 3 條測試，先因找不到 switch 全紅，再於實作後全綠。
- 最終驗證：pytest **4019 passed / 5 skipped**、vitest **1125 passed / 220 files**、
  TypeScript／ESLint／i18n 全綠、guard_all **43/44**（僅既知 Guard #23 npm audit 紅）。

### 5.6 P4 產出（2026-07-27 已落地）

- fallback 原始資料移至 `src/utils/permission_defaults.py`；該模組只依賴標準庫型別，
  不載入 Pydantic、PermissionService、ResourceManager 或任何應用單例。
  `permission_service` 僅把它投影為既有 `PermissionsConfig` 型別。
- fallback 刪除已退役的 `query_memory`／`search_vault`，並移除 Base 的 MCP 萬用授權
  與 Debugger→Owner 繼承；終態只有 Owner 的工具／MCP／技能維持 `["*"]`，Debugger、
  Guest、Base 與未知角色全部封閉。
- P0 矩陣已顯式記錄 P4 的刻意變更：fallback Debugger 的工具、技能與 MCP 由開放改封閉，
  Base MCP 由開放改封閉；生產設定所有 cell 不變，兩個工具閘門仍完全一致。
- Guard #32 現同時掃 `permissions.json`、`.example` 與 Python fallback，且工具名與 MCP
  條目形狀兩軸皆覆蓋。mutation 測試會在 fallback 植入 `ghost_tool`，並確認守衛變紅。
- pytest 與 standalone smoke sandbox 改播種生產 `permissions.json`；正常整合測試不再
  把異常降級模式誤當常態，fallback 由 P0 矩陣以不存在路徑專測。
- P5 的啟動 fail-fast／熱重載保留舊設定在下一節落地；P4 本身只把仍會被使用的
  fallback 收斂為可守衛、可預期的最小安全模式。
- 最終驗證：pytest **4019 passed / 5 skipped**；Guard #32 與其 mutation 測試全綠；
  guard_all **43/44**（唯一紅燈仍為既知 Guard #23 npm audit，非 P4 回歸）。

### 5.7 P5 產出（2026-07-27 已落地）

- `PermissionService` 將啟動載入與執行期重載分成兩種明確語意。既有、非空且非 `{}` 的
  `permissions.json` 若無法解析，啟動期會直接重拋原始 JSON／Pydantic 例外並拒絕啟動；
  錯誤保留確切欄位，不以 fallback 掩蓋。
- 熱重載先解析成暫存模型，成功後才原子替換目前角色設定。解析失敗時不覆寫記憶體中的
  last-known-good，也不套 fallback；系統記錄 `AUDIT` warning，並把不可變健康快照標成
  `permissions_config_reload_failed`。下一次成功重載會自動清除 degraded 狀態。
- 檔案不存在、內容空白或 `{}` 仍是合法 bootstrap，使用 P4 的最小安全 fallback，
  且不標記 degraded。
- 公開的 `/health` 與 `/system_health` 改用 `HealthResponse`／`SystemHealthResponse`
  強型別合約；stale last-good 期間維持 HTTP 200 存活語意，但 `status="degraded"`，
  並回傳 `permission_config_degraded` 與 `permission_config_degraded_reason`。
- 新增 `test_permission_config_reload_resilience.py` 5 個 case，涵蓋啟動拒絕、熱重載
  last-good＋audit＋health＋復原，以及三種 bootstrap 形狀；相關權限／路由回歸
  **93 passed**。
- 最終驗證：pytest **4024 passed / 5 skipped**、vitest **1125 passed / 220 files**、
  OpenAPI schema sync、TypeScript 與 Ruff 全綠；guard_all **43/44**，唯一紅燈仍為
  既知 Guard #23 `npm audit`，非 P5 回歸。

### 5.8 P6 產出（2026-07-27 已落地）

- 新增 [ADR-0196](../adr/0196-permission-representation-convergence.md)，正式收錄
  wire 選項 α、不可變 Grant、`resolve_grants` 唯一解析器、deny self-only／全域地板、
  fallback 與啟動 fail-fast／熱重載 last-known-good 決策。
- ADR-0195 的狀態區與已知邊界已反向註記：保留 per-installation／per-role 兩軸，
  明示角色 deny、Owner 身分面與載入語意由 ADR-0196 修訂；ADR 索引補 0196。
- `ROLE_MODEL.md` 補齊權限設定生命週期；`SECURITY.md`、`security_auth_guide.md`
  同步 RBAC Grant 與故障語意，並修正安全指南仍宣稱「過期 token 可走 LDH」及把
  `auth_level` 當角色設定的舊描述。
- `SSOT_MASTER_MAP.md` 登記 `permissions.json`／`PermissionsConfig` wire SSOT、
  `permission_defaults.py` fallback 與 `resolve_grants` 執行期決策 SSOT；同步修正
  LDH 首裝限定。
- 補登 P5 新增的 `_permission_config_health` 單例重設 `SLF001` 豁免：
  `NOQA_REGISTRY.md` 現值 209、`SLF001` 70，Guard #19 恢復同步。
- 終態驗證：ADR 0001..0196 連續、索引齊全且修訂雙向一致；權限／ADR 目標組
  **163 passed**；完整 pytest **4024 passed / 5 skipped**；guard_all **43/44**，
  唯一紅燈仍為既知 Guard #23 `npm audit`。

## 6. 影響面盤點

**後端 P2 已改**：`models/permissions.py`、`services/permission_service.py`、
`utils/permission_grants.py`、`routers/debug.py`；既有 ToolLayer／SkillLayer 呼叫鏈
透過 PermissionService API 自動收斂，不需重刻解析邏輯。

**前端**：P2 因 additive 頂層欄位重生 `api.ts`；P3 已修改 `RbacMatrix.tsx` 與兩個
locale，三個 allow 軸現在皆可在開放式與明確清單間雙向切換。

**設定檔**：`permissions.json`／`.example` 的既有角色欄位在 α 下不變；
兩個新全域地板欄位可省略且預設為空。P4 只動 Python fallback 資料及測試 sandbox 種子。
（`.example` 與 live 目前**完全一致**，已實測。）

**可觀測性**：P5 讓 `/health`／`/system_health` 額外暴露權限設定熱重載健康度；
成功重載會自動恢復 healthy，啟動期的壞設定則在端點可服務前直接拒絕啟動。

**守衛／文件**：P2 已納入全域地板與 deny-all 形狀；P4 已讓 Guard #32 擴掃
fallback；P6 以 ADR-0196、ADR-0195 反向註記與 ADR 索引收口，安全／角色／SSOT 文件
皆對齊同一語意。

**測試**：`test_permission_service.py`(25)、`test_rbac_hierarchy.py`(5)、
`test_security_policies.py`(7)、`test_rbac_mcp_tool_inheritance.py`(9)、
`test_config_examples.py`(8)、`test_tool_layer_denied_note.py`(1) 與前端
`RbacMatrix.test.tsx`(3) 為主受影響面；P4 另更新 P0 矩陣、Guard #32 mutation，
並讓 pytest／smoke sandbox 使用生產權限設定。

**DATA_PROTECTION**：全程不觸碰 `data/` 下任何檔案。

## 7. 刻意不做

- **不改 wire／JSON 形狀**（§4.4 選項 β）——獨立提案。
- **不動角色模型本身**（ADR-0141 的 `human_role`／`agent_role` 拆分不受影響）。
- **不為技能軸補 deny**——今日 `can_use_skill` 無 deny 面，`SkillGrant` 保留欄位但
  設定檔不新增語意，等有實際需求再開。
- **不把軸 A（`enabled_tools`／`disabled_tools`）併進來**——ADR-0195 已論證軸 A 必須
  留在供給面，本提案不改變該分工。
- **不引入前綴 glob**（`create_*`）——ADR-0195 已否決，理由不變。

## 8. 風險

| 風險 | 緩解 |
|---|---|
| P2 一次改動四處閘門，回歸面大 | P0 矩陣凍結是硬前提；P2 不通過矩陣就不合併 |
| deny 改 self-only 可能移除某個沒被矩陣涵蓋的隱性保護 | P0 矩陣需涵蓋 **40 工具 × 4 角色 ＋ 技能 ＋ MCP** 全笛卡兒積，不抽樣 |
| P5 fail-fast 可能讓設定寫壞的使用者開不了機 | 只對「檔案存在且非空但解析失敗」fail-fast；錯誤訊息需指出確切欄位；bootstrap 路徑不受影響 |
| `global_denied_*` 是新語意，可能被誤用成第二個 RBAC | 文件明確界定為「全系統禁令地板」，且 Guard #32 納入形狀驗證 |

## 9. 驗收條件

1. P0 矩陣測試存在且涵蓋全笛卡兒積，P1–P6 每階段皆綠。
2. `permission_service` 中不再有任何 `role_key == "Owner"` 硬編。
3. `"*"` 在 `src/` 下的權限語意消費點只剩**一處**（`Grant` 的建構器）。
4. 植入變異可紅：①把 Gate 2 改回獨立實作 ②移除 deny 的 self-only ③在 fallback
   植入死字串 ④讓 `_load_config` 熱重載失敗時降級。
5. `guard_all` 43/44（Guard #23 依既有決議維持原狀）、pytest／vitest 全綠。
6. ADR 落地並在 `README.md` 索引，與 ADR-0195 互相反向註記。

---

## 附錄 A：本提案的事實來源

所有 §2 數字皆為 2026-07-26 在 3.12 venv 對生產程式碼實測所得，非靜態閱讀推論：
有效權限矩陣、兩個閘門對 Owner 的實際回傳、fallback 生效時 Base 的 MCP 掛載結果、
`DEFAULT_PERMISSIONS_CONFIG` 兩個名稱對 `tool_registry`(40) 的比對、
`permissions.json` 與 `.example` 的逐角色相等性。
