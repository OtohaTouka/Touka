# 提案：人類／Agent 權限面分離（Role Plane Separation，選項 C 完整欄位拆分）

> **狀態：✅ 已核准並實作完成／收官（決策 2026-07-09、P0–P7 全數落地 2026-07-10）。**
> 擁有者拍板採**選項 C**（一次修乾淨、型別層終態保證），絞殺者 P0–P7 逐階段落地、人類逐批
> commit。各階段成果見 [ADR-0141](../adr/0141-role-plane-separation.md)，終態 SSOT 見
> [ROLE_MODEL.md](../ROLE_MODEL.md)。以下為原始實作藍本（歷史保存）。

---

## 1. 一句話總結

`AgentIdentity.role` 是一個扁平字串欄位，同時裝**人類角色**（`admin`／`normal`）和
**agent 角色**（`Owner`／`Base`／`Guest`／`Debugger`），全系統約 **35 處權限閘**直接對
混合清單做字串比對、無視 `is_human`——本提案把它拆成 `human_role`＋`agent_role`
兩個互斥欄位，以**絞殺者模式**分階段遷移（每個中間態全綠可交付），終態由型別層
保證「人類角色永遠不可能出現在 agent 欄位」，讓這一類 bug 從「靠紀律防」變成
「結構上不可能」。

## 2. 問題陳述（已驗證核碼事實）

兩個本該分離的權限面被壓進同一個欄位：

| 面 | 值空間 | 真正的 SSOT | 現況 |
|---|---|---|---|
| 人類 | `admin`／`normal` | `users.json`（`user.py:9` 預設 `normal`） | 登入時 `user.role` 直接倒進 `AgentIdentity.role`（`identity_service.py:92-98`） |
| Agent | `Owner`／`Base`／`Guest`／`Debugger`（＋內部 `System`） | `permissions.json` | `get_role_key()` 解析後倒進**同一個**欄位（`identity_service.py:101-104`） |

三個支點事實（2026-07-09 盤點覆核）：

1. **`identity.py:27-28`**——`role: str` 單一扁平欄位＋`is_human: bool` 是唯一的
   分辨訊號；但約 35 處閘完全不讀 `is_human`。
2. **`identity.py:68`**——`VALID_ROLES = {"Owner","Debugger","admin","Base","Guest"}`
   是矛盾的聯集：含人類 `admin`、**漏人類 `normal`**。後果：normal 人類登入後
   `role="normal"` 不在任何閘清單、也對不到任何 permissions.json profile——
   **normal 人類靜默落在所有特權閘之外**（只能靠 ownership／is_human 通過）。
   同時 identity_map 寫入驗證（`identity.py:80-98`）會接受 `default_role="admin"`
   ——人類角色可被指派給 agent。
3. **`identity_router.py:105`**——人類 admin 代打自有 agent 時，把 `"admin"` 字串
   貼到 `is_human=False` 的代打身分上（刻意，為保留管理權）——人類角色值直接
   漏進 agent 執行脈絡，是最大的混淆熱點。

**這是已釀成真實漏洞的危害類別，不是美觀問題**：

- `PROXY_INTERFACE.md:96-97` 記載——`role in {Owner,Debugger,admin}` 扁平比對正是
  一個 soul 讀取/匯出/**覆寫**越權（IDOR）的根因，靠補 `is_human`／`is_owner` 修掉。
- 2026-07-07 Context Snapshot「內容全空白」事故＝三個 debug 端點角色閘漏 `admin`
  （IDOR 修復時原本有、重寫時遺失）——混合清單靠人腦維護必然再犯。
- 系統今天能運作，**只因為值空間碰巧不相撞**（沒有 agent 叫 "admin"、沒有人類叫
  "Owner"）——這是巧合，不是設計。

## 3. 現況地圖（盤點摘要，2026-07-09 四維度並行盤點）

### 3.1 混合閘：約 35 處（16 個具名 `_*_ROLES` frozenset＋19 處 inline）

| 清單形狀 | 出現處 | 語意 |
|---|---|---|
| `{Owner, Debugger, admin}` 三元組 | 11 個 router frozenset（editor/connections/autonomy/custom_models/terminal/skills/scheduler/models/mcp/observability/identity 讀閘）＋ debug.py:43/140/186/240/323 inline ＋ souls.py:95/145 ＋ ui_sessions.py:775 ＋ ToukaMemory mission_manager.py:279/358 | 特權人類**或**特權 agent 壓成一個字串測試 |
| `{Owner, admin}` | creation（router＋tool 面）/feedback/skills.`_OWNER_ROLES`/bots ＋ mcp.py:445、group_settings.py:80、debug.py:335（reset） | 同上；`_OWNER_ROLES` 命名把 agent 角色 Owner 和人類「擁有」概念混為一談 |
| `{admin, Debugger}` | ui_sessions.py:85/119/144/169 ＋ resource_manager.py:500 | 人類 admin＋agent Debugger 當「看/改一切」旁路（Owner 刻意不在，落到 is_owner） |
| 單值 `admin`（人類閘） | permissions.py:24、admin.py:29、config.py:26、system.py:55、identity.py:42、ui_sessions.py:62、souls.py:28（唯一有配 is_human）、monitor.py:99 | 依賴「只有人類持有 admin」的隱性不變式 |
| 單值 agent 閘 | chat_upload.py:36（拒 Guest）、debug.py:246（Debugger 工具收窄）、identity_router.py:117 | 純 agent 面，拆分後機械改名 |

### 3.2 乾淨樣板已存在（redesign 的模板）

`resource_manager.py` 三處是**唯三**正確按 is_human 分支的地方：
`get_vault_root`（:222-226）、`is_owner`（:278-343）、`list_all_agent_stats`（:354-358）
——`is_admin = is_human and role=="admin"` vs `is_privileged_agent = not is_human and
role in ["Owner","Debugger"]`。目標模型就是把這個樣板變成型別結構。
（殘餘不一致：`is_owner:280` 的 Debugger 旁路**沒有** `not is_human` 防護，與姊妹
函式不同——列入 §8 刻意修正清單。）

### 3.3 其他關鍵事實

- **JWT token 不帶 role**——role 每次請求由 users.json／permission map 現場解析。
  ⇒ **遷移不需 token 失效、不需重登入、無持久化資料要遷移**（users.json／
  identity_map／permissions.json 的值空間本來就分離、不動）。
- **前端已經是兩平面**：人類角色（`UserOut.role`）與 agent 角色（`PermissionConfig`
  ／`IdentityMap`）本來就是分開的型別欄位、無壓平 union；所有 UI 閘只查人類
  `admin`。⇒ 前端幾乎零改動（僅 `IdentityMapUpdateRequest` 驗證集變窄 → openapi
  重生＋sync-api）。
- **Debugger 真相**：「DebugAgent 專用」是政策不是機制——LDH 對同網段無 token
  客戶端、只要宣稱 `X-Agent-ID: DebugAgent` 即發 Debugger＋真 token（
  `identity_router.py:149-178`，30 次/60s 限流）；`is_human` 恆 False；權限面
  繼承 Owner。兩個閘**刻意排除** Debugger（`_require_reset_role`、`_BOTS_ROLES`，
  ADR-0104 姿態）——真實的 human-vs-agent 意圖，遷移必須保留。
- **文件是最嚴重的混淆處**：`SECURITY.md:38` 把整個授權模型講成「四層授權
  （Owner/Debugger/Base/Guest）」——只列 agent 角色、漏掉人類 admin/normal；
  `security_auth_guide.md:60` 事實錯誤（寫「admin, user」且稱人類角色住在
  permissions.json）。**全 repo 沒有一份權威的角色模型文件。**
- **ToukaMemory 共用同一物件**：mission_manager 兩個閘讀的是 proxy 傳入的同一個
  `AgentIdentity`（in-process），必須與 proxy 同步遷移。
- **role 不只影響 allow/deny，還影響 vault 路徑路由**（Owner→personal_drive、
  非 Owner→data/custom；`_PathIdentity` 跨身分 bug 即此）——特徵化矩陣必須涵蓋。

## 4. 目標與非目標

**目標**
1. 型別層分離：`human_role` 與 `agent_role` 互斥欄位＋建構驗證器，人類角色
   結構上不可能進 agent 欄位（反之亦然）。
2. 全部 35 處混合閘改為顯式雙面判定（沿 resource_manager 樣板）。
3. `normal` 成為一等角色值（有明確語意與文件，即使權限為零）。
4. 代打（identity assumption）從字串走私改為顯式建模。
5. 一份權威 `ROLE_MODEL.md`＋修正所有文件的角色敘述；防漂移守衛永久化。

**非目標（範圍外，明確不做）**
- 不改 LDH 發卡姿態（同網段宣稱 DebugAgent 即得 Debugger 是另一個安全議題，
  另案處理）。
- 不改 permissions.json 的 agent 角色值空間與繼承結構。
- 不改 `User` 模型與 users.json schema（人類面本來就乾淨）。
- 不擴充 normal 人類的產品權限（normal 目前過不了任何特權閘＝現狀，維持；
  要給 normal 什麼權限是產品決策，另案）。
- 不重設計前端（已是兩平面）。

## 5. 選項比較與選定

| | A：只修文件＋守衛 | B：語意謂詞層 | **C：完整欄位拆分（選定）** |
|---|---|---|---|
| 修 IDOR 類危害 | ❌ 程式仍混 | ✅ 謂詞內建 is_human | ✅ **型別層強制** |
| 再犯可能 | 高 | 低（謂詞可被繞過，靠守衛盯） | **結構上不可能** |
| churn／風險 | 最小 | 中 | 最大（但見 §6 絞殺者機制） |
| 動 wire/持久化 | 否 | 否 | **實查為輕**（token 不帶 role；見 §7.5） |

擁有者拍板理由：「一次修乾淨」——B 的謂詞終態仍允許有人寫裸字串比對（只能靠
守衛抓），C 的終態讓這類 bug 無法被寫出來。C 多付的成本集中在模型、組裝點、
拆舊三個階段；35 處閘的遷移工作量 B/C 相同。

## 6. 為什麼 C 能安全分階段：絞殺者機制（本提案的核心保證）

1. **P1 加新欄位、不拆舊**：`AgentIdentity` 加 `human_role`／`agent_role`；舊
   `role` 變**計算屬性**（`is_human` 為真回 `human_role`、否則回 `agent_role`）；
   建構子加相容驗證器（收到舊式 `role=` 自動填進正確欄位）。
   ⇒ **落地當下 0 個呼叫點需要改、全測試照常綠**。
2. **逐批遷移**：一個 router 一批、一次 commit、每批全套綠。改壞哪批，範圍就是
   哪批。
3. **棘輪守衛**（隨 P1 上線）：維護「尚未遷移檔案」清單，只准縮不准長——新寫的
   碼不能用回 `.role`，停擺會被看見。
4. **終態拆舊**：全部遷完後移除 `.role` 屬性與相容建構子，守衛升級為永久禁令。

**關鍵性質：「停擺」的最壞後果是未完成，不是壞掉**——每個中間態都是行為等價、
全綠、可 commit 的合法狀態。P2–P4 之間停多久都行。

## 7. 目標模型設計

### 7.1 欄位與不變式

```python
# models/identity.py（終態示意）
HUMAN_ROLES = frozenset({"admin", "normal"})
AGENT_ROLES = frozenset({"Owner", "Base", "Guest", "Debugger", "System"})  # System 見拍板點3

class AgentIdentity(BaseModel):
    agent_id: str
    is_human: bool = False          # 語意：本請求的行動主體是「直接的人類會話」
    human_role: str | None = None   # ∈ HUMAN_ROLES；「本請求背後的人類權威」
    agent_role: str | None = None   # ∈ AGENT_ROLES；「執行中的 agent 軀體」
    ...

# 建構驗證器強制不變式：
#   is_human=True  ⇒ human_role ∈ HUMAN_ROLES 且 agent_role is None
#   is_human=False ⇒ agent_role ∈ AGENT_ROLES；human_role 可為 None 或（代打時）駕駛員的人類角色
#   human_role ∉ AGENT_ROLES、agent_role ∉ HUMAN_ROLES（跨面即 ValidationError）
```

三種合法形態：

| 形態 | is_human | human_role | agent_role | 例 |
|---|---|---|---|---|
| 直接人類 | True | admin／normal | None | 網頁登入 |
| 純 agent | False | None | Owner／Base／Guest／Debugger | soul 對話、心跳、子代理 |
| 代打（pilot pair） | False | 駕駛員角色 | 目標 agent 角色 | 人類駕駛自有 agent |

### 7.2 代打（assumption）顯式建模——取代 `identity_router.py:105` 的字串走私

代打身分其實是**一對**（人類駕駛員＋agent 軀體）。目標：`agent_role=目標 agent
的角色`＋`human_role=駕駛員的人類角色`。admin 閘改讀 `human_role=="admin"`（不論
is_human）＝行為保持（admin 代打仍有管理權）；非 admin 駕駛員的 `human_role=
"normal"` 不通過任何特權閘＝行為保持（今天 normal 代打即無 admin 權）。
好處：閘不再需要知道「這是代打」，讀對欄位即得對答案。

### 7.3 閘的目標寫法（resource_manager 樣板的型別化）

```python
# 遷移前（混合）：
if identity.role not in ["Owner", "Debugger", "admin"]: deny
# 遷移後（顯式雙面）：
if not (identity.human_role == "admin"
        or identity.agent_role in ("Owner", "Debugger")): deny
```

兩個刻意排除 Debugger 的閘（reset／bots）沿現狀：agent 面只允許 `("Owner",)`。
`_require_admin_only` 類人類閘改讀 `human_role == "admin"`——順帶結構性堵死
「非人類帶 role='admin'」這個今天理論上可過的洞（見 §9 矩陣註記）。

### 7.4 權限引擎與路徑路由

- `permission_service.is_authorized`／`get_allowed_tools` 以 `agent_role` 為鍵查
  permissions.json（人類 `human_role` 不進工具表＝現狀保持：人類特權走 router
  閘與 is_owner，不走 RBAC 工具表）。
- vault 路徑路由（`get_vault_root`／`_PathIdentity` 類）改讀 `agent_role`——
  特徵化矩陣涵蓋路徑解析（不只 allow/deny）。

### 7.5 序列化與跨界面（實查結果：比預想輕）

- **JWT**：不帶 role，零遷移。
- **users.json／identity_map／permissions.json**：值空間不變，零遷移。
- **ToukaMemory**：讀同一個 in-process 物件——mission_manager 兩閘與 proxy 同批遷。
- **`model_dump()` 消費者**（遙測、debug 輸出讀 dump 的 `"role"` 鍵）：實作時
  grep 窮舉；過渡期計算屬性可入 dump（`@computed_field`）維持相容，終態移除時
  同步清消費者。
- **前端**：僅 `IdentityMapUpdateRequest` 驗證集變窄（只收 AGENT_ROLES）→
  `update_openapi.py`＋`npm run sync-api`（Guard #17）。

## 8. 拍板點（審核時請逐項核准）

1. **代打建模**：採 §7.2 雙欄位配對（推薦）；或改用 capability 標籤
   （`"human_admin" ∈ capabilities`）。推薦前者——閘讀欄位即可，不引入第二套
   查詢面。
2. **非 admin 駕駛員的 human_role**：代打時填駕駛員真實角色 `"normal"`（推薦，
   語意誠實、行為中立——normal 過不了任何閘）；或填 None（更保守）。
3. **`System` 角色**：`is_owner:285` 用到但不在 VALID_ROLES——建議正式納入
   `AGENT_ROLES`（內部專用、identity_map 不可指派，validator 另限）；或維持
   隱形（不推薦，又是一個靠巧合的值）。
4. **刻意行為修正清單**（P6 逐條紅→綠，各自獨立 commit）——請逐條核准：
   a. `VALID_ROLES` 拆成 `HUMAN_ROLES`／`AGENT_ROLES`；identity_map 寫入只驗
      AGENT_ROLES（堵「把 admin 指派給 agent」）。
   b. `is_owner:280` Debugger 旁路補 `not is_human` 防護（對齊姊妹函式）。
   c. `resource_manager.py:500` `{admin,Debugger}` 拆顯式雙面判定。
   d. 結構性收斂：`agent_role` 永遠不可能是 `"admin"`（validator）——原本
      理論上可過 `_require_admin_only` 類閘的非人類 admin 身分從此不可建構。
5. **欄位命名**：`human_role`／`agent_role`（推薦）；或 `user_role`／`rbac_role`。
6. **守衛編號與形式**：Guard **#41**「Role Plane Guard」——遷移期＝棘輪
   （未遷移檔案清單只縮不長）；終態＝AST 禁 `.role` 屬性存取＋禁混合角色
   字面量集合出現在 routers/services。

## 9. 特徵化矩陣（P0，所有遷移階段的驗收基準）

一次性把現狀拍照成表格驅動測試：

- **維度**：每個閘（35 混合＋單面閘）×（role 值 × is_human）的**可達組合**
  →預期 allow/deny。
- **路徑路由**：`get_vault_root`／`get_vault_path` 類 ×（role × is_human）→
  預期路徑類型（personal_drive／data/custom／拒絕）。
- **代打矩陣**：admin 代打／normal 代打／agent 直呼 × 代表性閘。
- **驗收規則**：P2–P5 各批遷移後矩陣**一個位都不能變**；P6 每條刻意修正
  必須以矩陣 diff 形式呈現（紅燈先行）；不可達狀態（如非人類 admin）的結構性
  收斂在矩陣中明確標註為 intentional。

## 10. 分階段計畫（每階段：TDD 紅→綠＋ruff/lint_ssot/guard_all/pytest/vitest 全綠＋ADR＋人類 commit）

| 階段 | 內容 | 風險 | 驗收要點 |
|---|---|---|---|
| **P0** | 特徵化矩陣測試（權限＋路徑＋代打）＋拍板點定案＋棘輪守衛基線盤點 | 低（純加測試） | 矩陣覆蓋 §3.1 全清單；不改任何生產碼 |
| **P1** | 模型雙欄位＋不變式 validator＋`.role` 相容計算屬性＋建構子相容＋兩集合常數＋Guard #41 棘輪版 | 低 | **零呼叫點變動**；全套綠；矩陣不變 |
| **P2** | 遷移 routers 批次 1（debug/observability/souls/ui_sessions） | 中 | 矩陣不變；棘輪清單縮 |
| **P3** | 遷移 routers 批次 2（其餘 11 個 `_*_ROLES` router＋tools/builtin/creation） | 中 | 同上 |
| **P4** | 遷移 services（resource_manager 三樣板機械化＋:500）＋ToukaMemory 兩閘＋agent_reset／soul_service／monitor | 中 | 同上；vault 路徑矩陣不變 |
| **P5** | 身分組裝點直填新欄位（verify_token 雙分支／identity_router 三路徑／get_identity／handlers）＋代打配對落地 | **高**（單獨隔離） | smoke_ldh_handshake＋auth 全組；代打雙向測試（admin 可過管理閘 ✓／normal 不可 ✗） |
| **P6** | 刻意行為修正逐條（§8-4 清單，各自紅→綠獨立 commit） | 中 | 每條=矩陣顯式 diff |
| **P7** | 拆 `.role` 相容層＋建構子相容；測試收尾；Guard #41 升永久禁令；openapi＋sync-api；文件 SSOT（§11） | 低 | 全 repo 無 `.role` 存取；40+1 道守衛全綠 |

工程量估計：約 in-process 提案（4 階段）的 1.5–2 倍；P2–P4 可長間隔分批。

## 11. 文件 SSOT 修正（P7，與程式終態同批）

1. 新增權威文件 `docs/architecture/ROLE_MODEL.md`：兩面模型、三種身分形態、
   值空間、SSOT 對照表（users.json vs permissions.json）、代打語意、Debugger
   的政策邊界。
2. 修 `SECURITY.md:38`：「四層授權（Owner/Debugger/Base/Guest）」→ 兩面模型
   敘述（人類 admin/normal ＋ agent Owner/Base/Guest/Debugger）。
3. 修 `docs/guides/security_auth_guide.md:60`（事實錯誤：「admin, user」）與
   :137-145 閘表格改雙面寫法。
4. 修 `PROXY_INTERFACE.md:38/46/50/55` 與 `LEGAL_ARCH.md:90/92` 的混合列舉。
5. `<harness_context>`（ADR-0139）的 role 一行沿用——它印的是單一身分的當前
   角色值，拆分後改印 `agent_role`（子代理面）；Guard #40 承諾句不受影響。

## 12. 風險與緩解（三個真實風險）

1. **身分組裝點改壞 → 認證全掛（admin 自鎖門外）**。
   緩解：組裝點在 P1 完全不動（相容驗證器罩住）；P5 單獨隔離＋smoke 全跑；
   P5 前任何階段出事都與認證無關。
2. **代打配對設計錯誤 → admin 代打失權或權限外洩**。
   緩解：P0 拍板（§8-1/2）＋雙向測試先行；P5 才落地。
3. **行為偷渡——修 bug 混進機械遷移**。
   緩解：矩陣鐵律（遷移批矩陣零變動；修正條目=顯式 diff＋獨立 commit）；
   棘輪守衛防新碼走回頭路。

## 13. 誠實邊界

- **LDH 發卡姿態**（同網段宣稱 DebugAgent id 即得 Debugger token）是盤點另外
  揪出的獨立安全議題——本提案**不解決**，僅在 ROLE_MODEL.md 誠實記載現狀，
  另案評估。
- 本提案**不改變任何人的實際權限**（除 §8-4 列名的堵洞）：normal 過不了特權閘
  是現狀、遷移後依舊；要給 normal 什麼權限是產品題，另案。
- 「每個中間態可交付」的保證以**特徵化矩陣先行**為前提——矩陣沒寫完之前，
  任何遷移批都不得開工。
- 效益宣稱：本提案消除的是「混合字串閘」這一類缺陷的**再發生**；已存在的
  個別 bug（context-snapshot 類）已修，本提案是根治結構。

## 14. 相關

- 盤點依據：2026-07-09 四維度並行盤點（角色模型／35 閘站點／Debugger＋is_owner
  樣板／文件＋前端），本文件 §2-§3 全部錨點已逐行覆核。
- [ADR-0104]（Debugger 排除姿態：reset／bots）、[ADR-0132]（session owner 的
  human/agent 通道分離先例）、[ADR-0134~0138]（多階段提案的執行慣例先例）、
  [ADR-0139]（harness_context 的 role 顯示）。
- 既有正確樣板：`resource_manager.py` get_vault_root／is_owner／
  list_all_agent_stats；前端 UsersTab（人類角色的乾淨面）。
