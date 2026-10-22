# 守衛涵蓋缺口待辦（Guard Coverage Gap Backlog）

- **狀態**：🏁 **收官（24/24 全數落地，2026-07-16）**——Step 0 掃描的 24 條缺口全建機器執法：🏁 ADR-0162（HIGH G01-03）／0163（認證縫 G05/06/15/16/19）／0164（反漂移 G07/08/14/17）／0165（前端接線 G09/10/24）／0166（IDOR 家族餘波 G04/G18）／0167（部署 G12/G13）／0168（LOW 收官 G11/G20/G21/G22/G23）。**餘後續獨立工作**：早期 ADR 0001–0139 尚未做 Step 0 掃描（本輪僅 0140–0161）；ADR 對齊回顧線未啟動。G01／G02 的 Guard #42／#43 已於 2026-10-08 依 ADR-0238 隨發版資格機制退役（編號不重用）。
- **建立**：2026-07-16
- **範圍**：本文件是「守衛涵蓋審查」回顧線的產出待辦。列出**已承諾但無機器執法**的載重不變式，逐條標註缺口、失效模式、建議執法方向與批次。每條守衛建成後應另立 ADR（比照 ADR-0024/0029/0032/0034…），並回填本表狀態。
- **方法**：對 ADR 0140–0161 逐篇抽取載重不變式（共 154 條），查現有架構守衛（`scripts/guard_*.py`、前端 `guard:*`）與回歸測試覆蓋，凡聲稱「零執法」者再經**對抗式覆核**（獨立覆核盡力推翻）。基準：`guard_all.py` 41/41 全綠。
- **結果**：執法分布 test=116 / **none=27** / guard=4 / both=7。27 條零執法缺口經覆核＝**確認 24 條**、推翻 3 條假陽性（見附錄 B，不需建守衛）。

> ⚠️ 本次僅掃 ADR 0140–0161（系統長最快的一段）。早期 ADR 0001–0139 尚未做同樣掃描，缺口清單非全集。

---

## 橫貫發現（本次審查的核心結論）

這批 ADR 有一個**系統性反模式**：安全加固加入了不變式，但**執法只到「實作 + 一句註解」層，而既有測試往往把被保護的機制 mock 掉**，於是回歸時測試照樣全綠。實證：

- `test_ldh_graceful_downgrade` 整顆 mock 掉 `get_role_key`（G03）
- `AuthContext.logout.test` 整包 mock `@/lib/connections`（G06/G15）
- `test_ensure_configs_does_not_overwrite` 用的目標檔是 `skills.json`——**根本不在 `ESSENTIAL_CONFIGS`**，移除保護分支測試依然綠（ADR-0147 附帶）

**綠燈給了假安全感**，這比「完全沒守衛」更隱蔽。故本待辦的守衛設計原則：**必須真正驅動被保護的程式路徑並斷言其行為**，不得依賴「函式可被 import / 分類標籤存在」這類淺層鎖。

---

## 消化批次與優先序

依「可一起建置的守衛」分批，優先序 = 安全爆炸半徑。**批 1–3 屬守衛審 B 族（安全/身分/認證），批 4–6 屬 E 族（領域反漂移），批 7 為 LOW 收尾。**

| 批 | 主題 | 缺口 | 優先 |
| :-- | :-- | :-- | :-- |
| **1** | IDOR／身分家族（**綁做**） | G03, G04, G18 | 🔴 最高 |
| **2** | 發佈／分發安全 | G01, G02, G11 | 🔴 高 |
| **3** | 認證縫時序／授權 | G05, G06, G15, G16, G19 | 🟠 高 |
| **4** | 領域反漂移（自動化平面） | G07, G08, G14, G17 | 🟡 中 |
| **5** | 前端接線／技能審批 | G09, G10, G24 | 🟡 中 |
| **6** | 部署／設定細項 | G12, G13 | 🟡 中 |
| **7** | LOW 收尾 | G20, G21, G22, G23 | ⚪ 低 |

---

## 缺口總表

| ID | ADR | 嚴重 | 批 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| G01 | 0140 | 🔴 HIGH | 2 | 發佈管線 `needs: gate`／trivy／fresh-volume 閘序無守衛 | 🗑️ ADR-0162 Guard #42 已由 ADR-0238 退役（`release-images.yml` 已刪除） |
| G02 | 0140 | 🔴 HIGH | 2 | ghcr compose 純 pull 性（無 `build:`／源碼掛載）無斷言 | 🗑️ Guard #43 已由 ADR-0238 退役；改由 pytest 回歸鎖 |
| G03 | 0147 | 🔴 HIGH | 1 | LDH 無卡路徑刻意不帶類型旗標（防身分冒用）無測試 | 🏁 ADR-0162（pytest 回歸鎖） |
| G04 | 0141 | 🟡 MED | 1 | 混合角色字面量集合守衛（Guard #41 第二支）缺失 | 🏁 ADR-0166（Guard #41 第三支 AST） |
| G05 | 0142 | 🟡 MED | 3 | 步升 AUDIT 軌跡（記操作者、不記密碼）無測試 | 🏁 ADR-0163（pytest caplog） |
| G06 | 0144 | 🟡 MED | 3 | `touka_has_logged_in` 登出不得清除，無測試 | 🏁 ADR-0163（vitest） |
| G07 | 0145 | 🟡 MED | 4 | 靈魂模型註冊須經 model_manager（不得繞快取直寫） | 🏁 ADR-0164（快取權威+源碼守衛） |
| G08 | 0146 | 🟡 MED | 4 | headless 旗標沿父鏈繼承（子代理不得硬編 False） | 🏁 ADR-0164（繼承測試+docstring 校正） |
| G09 | 0149 | 🟡 MED | 5 | `skill_creation_approval_request` 前端白名單無 parity | 🏁 ADR-0165（fetchChatStream 驅動） |
| G10 | 0149 | 🟡 MED | 5 | StreamRegistryProvider 該事件路由無測試 | 🏁 ADR-0165（onEvent 路由斷言） |
| G11 | 0150 | 🟡 MED | 2 | heartbeat 執行期設定（含 token）gitignore 條目無守衛 | 🏁 ADR-0168（gitignore 存在性） |
| G12 | 0150 | 🟡 MED | 6 | CONFIG_FILE 解析須留在 awaken.py 直讀 env，無鎖 | 🏁 ADR-0167（源碼守衛） |
| G13 | 0151 | 🟡 MED | 6 | 探針鍵純度（不得被 compose environment 注入）無比對 | 🏁 ADR-0167（交集斷言） |
| G14 | 0152 | 🟡 MED | 4 | 伺服端 SSE 消費者須只讀 delta.content（排除 reasoning） | 🏁 ADR-0164（行為+源碼掃描） |
| G15 | 0153 | 🟡 MED | 3 | 認證卡寫入須先於 setIsAuthenticated，無時序鎖 | 🏁 ADR-0163（vitest 源碼序） |
| G16 | 0154 | 🟡 MED | 3 | auth_level 授權須在代打 re-bind 前判定，無測試 | 🏁 ADR-0163（pytest 源碼序） |
| G17 | 0157 | 🟡 MED | 4 | K1：`run_react_loop` 須把每家 send_round 經 pump 抽乾 | 🏁 ADR-0164（inspect 源碼守衛） |
| G18 | 0158 | 🟡 MED | 1 | create_soul **刻意不得**寫 identity_map override（Sumi footgun） | 🏁 ADR-0166（磁碟+記憶體雙查） |
| G19 | 0159 | 🟡 MED | 3 | EXEMPT provider 豁免前提（守衛只驗分類存在不驗正確） | 🏁 ADR-0163（parity 守衛擴充） |
| G20 | 0145 | ⚪ LOW | 7 | register/unregister_soul_model 冪等性無測試 | 🏁 ADR-0168 |
| G21 | 0145 | ⚪ LOW | 7 | registry_snapshot autouse fixture 無 meta-守衛 | 🏁 ADR-0168（meta-test） |
| G22 | 0145 | ⚪ LOW | 7 | register 須插「裸」條目（不寫 enabled/visibility） | 🏁 ADR-0168 |
| G23 | 0157 | ⚪ LOW | 7 | K1：提前收流須 cancel 孤兒任務，無測試 | 🏁 ADR-0168 |
| G24 | 0161 | ⚪ LOW | 5 | 截圖失敗三分支回饋（warning/error/靜默）無測試 | ⬜ 待建 |

---

## 批 1 — IDOR／身分家族（綁做，最高優先）

> 三條同屬「身分/角色被靜默提權或冒用」家族，共享 identity/role 心智模型，應一起設計、一起建。

### G03 · LDH 無卡路徑身分冒用（ADR-0147，🔴 HIGH）
- **不變式**：無卡首裝視窗（`get_optional_identity` 第 3 步、`X-Agent-ID` 分支）須維持 plain `get_role_key(target_id)`「刻意不帶類型旗標」。X-Agent-ID 為攻擊者可控。
- **失效模式**：有人為與已接線路徑一致而補上 `is_toukasoul` 旗標 → 攻擊者在未認領視窗以 `X-Agent-ID=<既存靈魂 id>` 解析成 Owner、鑄一張綁定該靈魂記憶 vault/home 的 Owner 卡＝身分冒用／提權。
- **現況執法**：❌ 無。驅動真實 `get_role_key` 的測試（smoke_ldh_handshake、test_identity_graceful_reissue）全用 DebugAgent（override→Debugger，對旗標不敏感）且不建靈魂；其餘全 mock `get_role_key`。
- **建議方向**：新增回歸測試——以**無卡 + `X-Agent-ID=<既存 ToukaSoul id>`** 走真實無旗標解析路徑，斷言解析為 `Base`（非 Owner）、且不得鑄具名靈魂綁定卡。考慮同時加 AST/lint 守衛，禁止 LDH 分支呼叫 `resolve_role_and_caps`（帶旗標版本）。

### G04 · 混合角色字面量集合守衛缺第二支（ADR-0141，🟡 MED）
- **不變式**：決策點 6 終態守衛第二支——「禁跨面混合角色字面量集合出現在 routers/services」（如 `display_role in {"Owner","admin"}`）。**此模式正是釀成 soul IDOR 的原始 bug 類。**
- **失效模式**：P1–P6 regex 棘輪版曾以 `_*ROLES` frozenset 近似看守，P7 升 AST 版時未重建此支 → 新引入的跨面混合閘可逃過 Guard #41。
- **現況執法**：❌ 無。Guard #41（`guard_role_plane.py`）只實作兩條 AST 檢查（`.role` 存取 + `AgentIdentity(role=…)`），無角色字面量集合偵測。
- **建議方向**：擴充 Guard #41 加第三條 AST 規則——偵測 routers/services 中「同一集合字面量混入 HUMAN_ROLES 與 AGENT_ROLES 成員」的比較式。

### G18 · create_soul 不得寫 identity_map override（ADR-0158，🟡 MED）
- **不變式**：`create_soul` **刻意不得**寫入 identity_map override（角色面由 ADR-0147 tag_rules `is_toukasoul→Owner` 涵蓋）。建立時寫 override＝製造「override 蓋 tag_rules」的降級風險。
- **失效模式**：**這正是 Sumi 降級事故的成因。** 負向不變式若被違反（有人在建立路徑加回 override 寫入），因寫入值通常＝Owner，不會讓任何現有測試變紅。
- **現況執法**：❌ 無。create_soul 測試只斷言 owned_agents/models/registry/brain，從不斷言 `agent_overrides` 為空。
- **建議方向**：新增回歸測試——create_soul 後斷言 `identity_map.agent_overrides` 不含新靈魂 id。可考慮 AST 守衛禁止 create 路徑呼叫 `update_identity`。

---

## 批 2 — 發佈／分發安全

### G01 · 發佈管線閘序無看守（ADR-0140，🔴 HIGH；🗑️ 已退役）
> 歷史條目：`release-images.yml` 與其 Guard #42 已依 ADR-0238 刪除，現行 `.github/workflows/ghcr-publish.yml` 只做最小建置推送。
- **原不變式**：發佈 job 必須在 secret 掃描與 fresh-volume 啟動閘之後才推送映像；該閘與守衛已隨發版資格機制一併移除。

### G02 · ghcr compose 純 pull 性無斷言（ADR-0140，🔴 HIGH；🗑️ 守衛已退役）
> 守衛已退役：Guard #43 已依 ADR-0238 刪除；`docker-compose.ghcr.yml` 純 pull 性仍由 pytest `tests/unit/test_docker_compose_config.py::test_pull_compose_uses_only_ghcr_images_without_source_mounts` 回歸鎖住。
- **不變式**：`docker-compose.ghcr.yml` 三服務必須全走 `image:`（`ghcr.io/...:${TOUKA_VERSION}`），不得含 `build:`、不得掛載任何 repo 源碼檔案。
- **失效模式**：dev 掛載回流 → pull 使用者（無 repo）首跑掛上空目錄／建置失敗。
- **現況執法**：上述 pytest 回歸鎖斷言每服務走 `image:`、無 `build:`、無源碼 bind mount。

### G11 · heartbeat 執行期設定 gitignore（ADR-0150，🟡 MED）
- **不變式**：root `.gitignore` 的 `heartbeat-config/`（＋相容 `heartbeat-config.json`）與 `ToukaHeartbeat/.gitignore` 的 `config/` 不得被移除——該產物含生成的 `heartbeat_token`。
- **失效模式**：移除任一忽略行 → 執行期設定（含 token）被靜默提交入版控（repo 有開源計畫＝token 洩漏）。
- **現況執法**：❌ 無。唯一讀 `.gitignore` 的測試只斷言「.dockerignore 不得被 gitignore」此不同不變式。
- **建議方向**：新增測試——`git check-ignore` 或字面掃描斷言該三條 gitignore 條目存在。

---

## 批 3 — 認證縫時序／授權

### G05 · 步升 AUDIT 軌跡（ADR-0142，🟡 MED）
- **不變式**：`_verify_admin_stepup` 成功/失敗皆須留 AUDIT log（記操作者與對象），且不得記錄密碼本身。
- **失效模式**：刪掉兩處 `logger.warning` 或誤把密碼加進格式字串 → 敏感變更無法追溯／密碼外洩，全套件照綠。
- **現況執法**：❌ 無（對照組 admin.py 的同類 AUDIT 有 `test_sec_batch_gamma` 源碼級釘住，souls.py 步升零測試）。
- **建議方向**：新增測試——用 `caplog` 斷言兩分支各留一筆 AUDIT log（含 operator/target），並斷言 log 內容不含密碼。

### G06 · `touka_has_logged_in` 登出不清除（ADR-0144，🟡 MED）
- **不變式**：`touka_has_logged_in` 為持久旗標，登出**不**清除（`clearAccessToken`/`logoutTouka` 不得移除 `HAS_LOGGED_IN_KEY`）。
- **失效模式**：登出後瀏覽器重新變回「首裝相容」→ 重新採納 X-New-Auth-Token 降級卡，殭屍身分防線的持久面靜默失效。
- **現況執法**：❌ 無。logout 路徑測試若非 mock 掉 `markHasLoggedIn` 就是完全不碰旗標。
- **建議方向**：新增測試——呼叫 `logoutTouka()`/`AuthContext.logout()`（不 mock connections）後，斷言 `HAS_LOGGED_IN_KEY` 仍在 localStorage。

### G15 · 認證卡寫入先於 setState（ADR-0153，🟡 MED）
- **不變式**：AuthContext 中 `storeToken`/`clearAccessToken` 寫 `_accessToken` 必須發生在對應 `setIsAuthenticated` **之前**，事件驅動重探才能於 dispatch 當下讀到正確卡況。
- **失效模式**：在兩者間插入 `await` 或重排 → 重探讀到過期/空卡 → `/v1/memory` 401 噪音 / mode 卡 standalone（ADR-0153 修的原 bug 復發）。
- **現況執法**：❌ 無。所有 AuthContext-family 測試 mock 掉 connections，`getAccessToken` 成 no-op。
- **建議方向**：新增測試——用真實（或 spy 版）`getAccessToken`，在 `touka_auth_changed` 監聽內斷言讀到的卡況已反映新狀態（登入讀到 token、登出讀到 null）。

### G16 · auth_level 授權須在代打 re-bind 前判定（ADR-0154，🟡 MED）
- **不變式**：MonitorLayer 對未授權 auto-mode 降級為 Manual 的閘，必須讀取**尚未換身分的人類 identity**（`is_admin_authority` 或 `target in owned_agents`）。
- **失效模式**：把 `_maybe_assume_soul_identity` 重排到此閘之前 → 授權改讀代打後靈魂身分（Owner、human_role 非 admin、owned_agents 通常空）→ auto-mode 靜默降級。
- **現況執法**：❌ 無。唯一測試只直接測 `_maybe_assume_soul_identity` 的擁有權 Gate-1，不觸及 auth_level 閘或其相對 re-bind 的順序。
- **建議方向**：新增測試——以真實人類擁有者身分（is_human=True、owned_agents 含目標）走 `MonitorLayer.process()`，斷言 auto-mode 不被降級；並驗證閘在 re-bind 之前執行。

### G19 · EXEMPT provider 豁免前提（ADR-0159，🟡 MED）
- **不變式**：ChatProvider 不得新增掛載時 fetchTouka 首探；StreamRegistryProvider 的 `/v1/ui/streams/active` 首探須續閘於 `sessions.length>0`。
- **失效模式**：前提被改壞 → provider 靜默重演「無卡首探 401 → 清單卡空／跨帳號殘留」，而 parity 守衛因分類仍在 EXEMPT 保持全綠（**守衛只驗分類存在、不驗分類正確**）。
- **現況執法**：❌ 無（`providers_auth_self_heal.test.ts` 只驗 EXEMPT 標籤存在）。
- **建議方向**：強化 parity 守衛或加測試——斷言 ChatProvider 無掛載期 fetchTouka、StreamRegistry 的 active 首探有 `sessions.length` 前置閘。

---

## 批 4 — 領域反漂移（自動化平面）

### G07 · 靈魂模型註冊不得繞快取（ADR-0145，🟡 MED）
- **不變式**：靈魂模型註冊/註銷必須經 `model_manager.register_soul_model`/`unregister_soul_model`（同時異動 `_models_cache` 與磁碟），永不得只寫 `models.json` 繞過快取。
- **失效模式**：改回直寫 → 背景 `_save_config`（check_models_health/mark_model_seen）以過時快取整包覆蓋、抹除 `ToukaSoul<id>`＝**正式環境資料流失**＋隨機次序 flake。
- **現況執法**：❌ 無。唯一 create 測試把 `_models_cache` 歸零使背景健檢早退，且只驗磁碟面。
- **建議方向**：新增測試——註冊靈魂模型 → 觸發 `check_models_health`/`_save_config` → 斷言 `ToukaSoul<id>` 仍在快取。考慮 AST 守衛禁止 souls 路徑對 `models.json` 直寫。

### G08 · headless 旗標沿父鏈繼承（ADR-0146，🟡 MED）
- **不變式**：`_run_subagent` 須把 `enable_autonomy` ctx 旗標沿父鏈繼承（`subagent_tools.py:193`），不得硬編 `enable_autonomy=False`。
- **失效模式**：改回硬編 False → heartbeat 子代理 `_headless` 變 False → `get_effective_rounds` 回 0（不限）→ **無人在場的子代理工具迴圈失控**（唯一保險絲失效）。同檔 docstring 仍寫舊語意，誘導重構者回寫。
- **現況執法**：❌ 無。兩個執行該行的測試皆以 `base_ctx={}` 呼叫（`bool({}.get())` 與硬編 False 不可區分）。
- **建議方向**：新增測試——以 `base_ctx={'enable_autonomy': True}` 呼叫，斷言子代理 ctx 繼承 True；順手修正 docstring 舊語意。

### G14 · SSE 消費者排除 reasoning（ADR-0152，🟡 MED）
- **不變式**：伺服端既有 SSE 消費者（dispatcher `_extract_delta_text`、heartbeat 轉錄、SNS gateway、subagent 收流）必須維持「只讀 `delta.content`」，reasoning-only chunk 被自然略過。
- **失效模式**：有人為某消費者加上 `reasoning_content` 處理 → 思考靜默漏進心跳轉錄／派工正文（原 bug 在自動化平面重演）。
- **現況執法**：❌ 無。四消費者對應測試全只餵 content chunk，無一餵 reasoning-only chunk 斷言萃取為空。
- **建議方向**：新增測試——對每個消費者餵一個「有 `reasoning_content`、無 `content`」的 chunk，斷言萃取結果為空。

### G17 · K1 send_round 經 pump 單一 call-site（ADR-0157，🟡 MED）
- **不變式**：`run_react_loop` 必須把**每個** adapter 的 `send_round` 都經 `pump_send_round` 抽乾（單一 call site，第 165 行），三家慢上游才同享心跳。
- **失效模式**：重構把包裹拿掉或新增旁路 → openai 長 POST／gemini 503 退避／claude SDK 呼叫的心跳靜默消失、前端久候斷線。
- **現況執法**：❌ 無。唯一涉 pump 的測試只隔離測 pump 本體（直呼，非經 loop）；golden 在即時 mock 下心跳不觸發（拿掉包裹仍逐位元相同）。
- **建議方向**：新增測試——以慢 mock adapter（send_round 延遲 > keepalive 閾）走 `run_react_loop`，斷言 keepalive 心跳被送出（證明真經 pump）。

---

## 批 5 — 前端接線／技能審批

### G09 · skill_creation 前端白名單 parity（ADR-0149，🟡 MED）
- **不變式**：`connections.ts` 的 `dispatchSseLine` `customEventTypes` 白名單必須含 `"skill_creation_approval_request"`。
- **失效模式**：被移除/改名 → 後端已發的部署審批事件被前端丟棄 → 無人解算 → 180s auto-reject「你拒絕了」＝互動部署靜默失敗。
- **現況執法**：❌ 無。白名單與後端事件型別無任何程式同步機制；`guard_tool_events.py` 只掃後端五型別、不做前後端 parity。
- **建議方向**：新增測試——透過 `dispatchSseLine` 實際餵入該事件，斷言不被丟棄（觸發對應 handler）。或加前後端事件型別 parity 守衛。

### G10 · StreamRegistry 事件路由（ADR-0149，🟡 MED）
- **不變式**：`StreamRegistryProvider.onEvent` 必須把 `skill_creation_approval_request` 與 `skill_install_approval_request` 一同路由到 `onSkillInstallRequest`（→ EvolutionModal）。
- **失效模式**：OR 條件退化（漏掉 creation 型別）→ 事件過白名單卻無 handler → 審批卡不彈、部署逾時靜默失敗。
- **現況執法**：❌ 無。StreamRegistry 測試從不用 skill 事件驅動 onEvent；EvolutionModal 測試直接把 request 傳給 modal 繞過路由。
- **建議方向**：新增測試——餵 `skill_creation_approval_request` 給 `onEvent`，斷言 `onSkillInstallRequest` 被呼叫。

### G24 · 截圖失敗三分支回饋（ADR-0161，⚪ LOW→本批）〔🏁 ADR-0165 源碼掃描〕
- **不變式**：`handleScreenshot` 於 `getDisplayMedia` API 缺席須 `toast.warning`；擷取錯誤須 `logSSOTError`+`toast.error`；僅使用者主動取消（`NotAllowedError`）維持靜默。
- **失效模式**：三分支回歸成「直接 return／catch 全吞」→ 使用者點相機毫無回饋＝體感按鈕壞了。
- **現況執法**：❌ 無。唯一相關測試斷言的是 `vi.fn` mock；Guard #33 只驗 i18n key 存在。
- **建議方向**：新增測試——三分支各觸發一次，斷言對應 toast/logSSOTError 被呼叫、NotAllowedError 不觸發 toast。

---

## 批 6 — 部署／設定細項

### G12 · CONFIG_FILE 解析留在 awaken.py 直讀 env（ADR-0150，🟡 MED）
- **不變式**：`HEARTBEAT_CONFIG_FILE` 的解析刻意在 `awaken.py` 直讀 `os.environ`、不經 `config.py`（config.py 已烤進映像、dev compose 只 live-mount awaken.py）。
- **失效模式**：把 env 讀取重構進 config.py → 功能測試仍全綠，但 dev「`up -d` 免 `--build` 即生效」熱修路徑靜默失效。
- **現況執法**：❌ 無。唯一測試只驗純函式 `_resolve_config_file`，不斷言 env 讀取須留在 awaken.py。
- **建議方向**：新增測試/守衛——斷言 `awaken.py` 內有 `os.environ.get('HEARTBEAT_CONFIG_FILE')`、且 config.py 不解析此鍵。（權衡：此條偏 dev 工作流保護，可評估是否值得建。）

### G13 · 探針鍵純度（ADR-0151，🟡 MED）
- **不變式**：`_ENV_PERSISTENCE_PROBE_KEYS` 只能收錄「只來自 .env、絕不由 proxy compose `environment:`/`-e` 注入」的鍵。
- **失效模式**：混入任何 compose 注入鍵 → 該鍵在 os.environ 恆存在卻可能合法不在健康 .env → 幽靈檔守衛判準靜默誤報/漏報（安全網給錯誤訊號）。
- **現況執法**：❌ 無。無任何測試把 `config.py` 的 tuple 與雙 compose 的 `environment:` 清單做交集為空的交叉斷言。
- **建議方向**：新增測試——parse 雙 compose 的 proxy `environment:`，斷言與 `_ENV_PERSISTENCE_PROBE_KEYS` 交集為空。

---

## 批 7 — LOW 收尾

### G20 · register/unregister 冪等性（ADR-0145，⚪ LOW）
- **不變式**：重覆註冊已存在項＝no-op；反註冊不存在項＝零寫入 no-op（create 回滾與刪不存在項所依賴）。
- **現況執法**：❌ 無（間接端點覆蓋全用可容重複的 `any()`/`not any()` 斷言）。
- **建議方向**：新增單元測試直測兩函式的冪等性。

### G21 · registry_snapshot fixture defense（ADR-0145，⚪ LOW）
- **不變式**：`registry_snapshot` autouse fixture 必須無條件把 models.json/agents_registry.json 佈為 `{}` 並呼叫 `load_initial_config()` 歸零快取（阻止背景健檢跨測試回寫覆蓋、消除隨機次序 flake）。
- **現況執法**：❌ 無（`test_soul_admin_stepup.py:37` 的分歧姊妹 fixture 正證明無此不變式被執法）。
- **建議方向**：新增 meta-測試或統一 fixture 定義，斷言其無條件行為。

### G22 · register 插「裸」條目（ADR-0145，⚪ LOW）
- **不變式**：`register_soul_model` 必須插入 `{"id":"ToukaSoul<id>","owned_by":"touka"}`，不得把 enabled/visibility 寫入快取/磁碟（此二欄由 get_models 讀取時補預設）。
- **現況執法**：❌ 無。
- **建議方向**：新增測試斷言持久化條目的鍵集恰為 `{"id","owned_by"}`。

### G23 · K1 孤兒任務 cancel（ADR-0157，⚪ LOW）
- **不變式**：消費端提前收流（GeneratorExit）時，`pump_send_round` 的 `finally` 需 cancel 未完成的 `next_task`（避免殘留懸掛的 asyncio 任務，含仍在飛的上游 LLM 請求）。
- **現況執法**：❌ 無。唯一測試完整抽乾 generator，無 GeneratorExit/cancel 斷言；`pytest.ini` 無 `filterwarnings=error` 故孤兒任務警告不翻紅。
- **建議方向**：新增測試——generator 中途 `aclose()`，斷言 `next_task` 被 cancel。

---

## 附錄 A — 建置慣例

- 每條缺口的守衛/測試建成後，另立 ADR（比照既有守衛 ADR：0024/0029/0032/0034/0068/0088/0098/0102…），並回填本表「狀態」欄為 🏁 ADR-XXXX。
- 新增架構守衛須納入 `scripts/guard_all.py` 並更新其編號註解與計數。
- 守衛設計鐵則（見上「橫貫發現」）：**必須真正驅動被保護的程式路徑並斷言其行為**，禁止依賴「函式可 import／分類標籤存在」的淺層鎖。

## 附錄 B — 被對抗覆核推翻的假陽性（不需建守衛）

| ADR | 原聲稱 | 為何不成立 |
| :-- | :-- | :-- |
| 0141 | permissions.json admin/normal 角色鍵無 lint | **型別系統已封死**：`_enforce_plane_purity` 使 `agent_role∈{admin,normal}` 結構性不可達；工具政策只以 `agent_role` 為鍵；`for_role` 把 override 的 `role="admin"` 路由進 `human_role`。提權面不成立（有 pytest 覆蓋）。 |
| 0151 | `verify_env_persistence_integrity()` 啟動接線無執法 | **Guard #20（vulture 死碼）間接鎖**：刪 `main.py:189` 呼叫使 line 181 import 變孤兒 → 90% 信心報 unused → Guard #20 fail。⚠️ **但有漏洞**：同時刪 181+189 的乾淨移除只有 60% 信心會逃逸——間接鎖不牢，**若要保險仍可補直接守衛**（列為選配）。 |
| 0156 | send_round 失敗須回 RoundError 無測試 | `tests/proxy/test_failure_visibility.py::test_openai_handler_network_error_visible` 已驅動該路徑並斷言優雅降級（部分：僅 OpenAI transport-error 路徑）。 |

---

*本文件由 Step 0 缺口掃描（含對抗式覆核）產出。完整逐條覆核裁決的原始輸出未入版控。*
