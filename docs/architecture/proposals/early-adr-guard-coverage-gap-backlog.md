# 早期 ADR 守衛涵蓋缺口待辦（Early-ADR Guard Coverage Gap Backlog，0001–0064）

- **狀態**：🏁🏁 **0001–0139 全掃完（除退役 0072）＝掃描階段收官（2026-07-23）**。**全線總帳＝76 條 HIGH/MED 確認（6 HIGH／70 MED）＋~105 LOW 面值＋7 推翻**（E01-E34〔0001-0064〕+E35-E77〔0065-0139，除 E50=E45 重複〕）。詳見文末「🏁🏁 全線收官」。**🏁 批 LIVE（E35/E65/E76 三條 LIVE 生產缺口）全數修復＝ADR-0175（2026-07-23，owner 拍板①）。**🏁 批②＝ADR-0176（07-23）E39/E66 行為級測試釘死＝掃描 6 HIGH 全數收官。**🏁 批③＝ADR-0177（07-23）claude_adapter `_claude_request` fixture 盲點群 4 測全關（E54/E61/E72/E73 四 MED＋觸及 0128/0129 LOW 同點）。**🏁 批④＝ADR-0178（07-23）E77 路徑穿越回歸測試釘死（near-HIGH 安全收口）。餘批（⑤家族③⑦系統批）待 owner 另拍板。****三高槓桿治本全數收官（owner 拍板順序 ②→④→①，皆 2026-07-17）：🏁 批②（E02＋E17）＝ADR-0169、🏁 批④（E05/E06/E07）＝ADR-0170、🏁 批①（E09）＝ADR-0171（Guard #44）**。**🏁🏁 批 A 4 條 HIGH 全數收官：E02＝ADR-0169、E01/E03/E04＝ADR-0172（三支行為級測試，2026-07-19）。** **🏁🏁🏁 修碼線全線收官（2026-07-24）＝14 批（ADR-0169～0172／0175～0184）76 條 HIGH/MED 全數處置：4 生產修（E35/E65/E76/E75）＋2 守衛強化（Guard #44／FileLockAsyncRule check_Call）＋其餘行為級回歸測試釘死；E23 🏁 ADR-0185（CI 所有權探針）、~105 LOW 留檔備查＝**76+1 全數處置**。**
- **建立**：2026-07-17
- **範圍**：「守衛涵蓋審查」回顧線對**早期 ADR** 的延伸（前作 `guard-coverage-gap-backlog.md` 僅掃 0140–0161）。本輪掃 ADR 0001–0064，逐篇抽載重不變式、查現有守衛/測試覆蓋，凡「零執法」者經**對抗式覆核**（獨立代理盡力推翻＋變異測試：mutate 被保護的那一行，看測試是否仍全綠）。
- **結果（0001–0064）**：**34 條 HIGH/MED 確認缺口**（4 HIGH／30 MED）＋ ~57 條 LOW（11 已覆核＋~46 面值）＋ **4 條推翻假陽性**（附錄 A）。方法同前：基準 `guard_all.py` 43/43 全綠。
- **原始資料**：session scratchpad `early_c1_FINAL.md`、`early_c2.md`…`early_c6.md`（每條含覆核裁決與 file:line 證據）。

> 🏁 **掃描全集完成**：ADR 0001–0139（除退役 0072）全數掃過。續掃用的可重用 workflow＝`early-guard-coverage-scan`（analyze=sonnet→verify=opus，縮塊 7 篇/run、high/med 都覆核）；scratchpad early_c1_FINAL/early_c2..c17.md 為逐塊逐條原始資料。**下一步＝owner 拍板修碼分批（見文末優先序）。**

---

## 橫貫發現（8 個系統性家族）

早期 ADR 的假綠不是隨機散佈，而是收斂成 **8 個可命名、可治本的反模式**。守衛設計原則同前：**必須真正驅動被保護的程式路徑並斷言其行為**，源碼序鎖/字串鎖/mock 掉被保護機制/只驗分類標籤都不算執法。

| # | 家族 | 代表缺口 | 治本方向 |
| :-- | :-- | :-- | :-- |
| ① | **ruff 宣告卻零執法**：ruff.toml 有 banned-api 但掃描當時 ruff 不在 guard_all、也不在 CI（當時的 release workflow 只跑 docker/trivy/boot，現已依 ADR-0238 刪除） | 0004×2、0017、（0056 as any） | **把 `ruff check` 納入一道 guard_all 守衛**，一次啟用所有 banned-api |
| ② | **smoke_*.py 被 pytest 排除＝安全假綠**：關鍵防護住在 `tests/smoke/*.py`，`pytest.ini python_files=test_*.py` 從不收集、guard_all 不引、CI 不跑 | **0022 path-escape（HIGH）**、0007 | **關鍵 smoke 升格 `test_` 或納 guard_all 執行** |
| ③ | **源碼序/字串鎖、生產路徑無測**（最核心、最普遍）：測試只 inspect.getsource 對字串/順序、或只測 pure helper、或停在 404/403 前置分支，從不執行被保護的那一行 | G16/**0021**、0033、0057、0061×2、0062、0064、0046… | 測試須 driver 真路徑並斷言行為；變異一行應變紅 |
| ④ | **守衛只查『存在』不查『值/等級/範圍』**：Guard #22/#24 端點認證守衛 `_AUTH_KEYWORDS` 收 `get_verified_identity` 為足夠＝只抓「完全無認證」不抓「admin→authenticated 降級」 | **0034、0035、0053**、0032 | **強化 Guard #22/#24 辨識 `require_admin` vs `get_verified_identity`＝一次關整族** |
| ⑤ | **唯一行為測試實證假綠**：拔掉被保護機制，那條「行為測試」竟仍全綠 | 0028 bcrypt（拔 prehash 兩斷言仍過） | 需實跑變異驗證，不可只讀碼判斷 |
| ⑥ | **假綠 via 測試死碼**：被測的函式其實 production 不呼叫（grep 只在測試檔） | 0041 logout（測 `authService.logout` 死碼） | 查被測函式在生產是否有呼叫端 |
| ⑦ | **跨行程/併發不變式被 in-process 測試遮蔽**：同一事件迴圈內 asyncio.Lock 自己就序列化，讓 file lock 形同虛設仍全綠 | 0049 AsyncFileLock | 併發正確性須跨行程測試 |
| ⑧ | **不對稱姊妹涵蓋**：成對/鏡像不變式只守一半 | 0062（auth_level 有測、active_skills 無）、0064（WRITE 閘有測、READ 鏡像無）、0033（startswith 姊妹）、0017（append vs insert）、0027（ValueError vs TypeError） | **查缺口捷徑：鏡像/姊妹其一有測就查另一半** |

---

## HIGH／MED 缺口總表（34 條）

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E01 | 0021 | 🔴 HIGH | ③ | auth-level 降級守衛無測（G16 是源碼序鎖，無測呼叫 `process()`） | 🏁 ADR-0172（test_monitor_authlevel_gate.py 真呼叫 process()＋閘序行為證明） |
| E02 | 0022 | 🔴 HIGH | ② | path-escape 沙盒 `_enforce_root_bound` 只在 smoke（pytest 不收集） | 🏁 ADR-0169（test_io_prison.py 升格＋接線層） |
| E03 | 0044 | 🔴 HIGH | ③ | mission board 跨身分 IDOR else 分支（`mission_manager.py:54`）無測 | 🏁 ADR-0172（test_mission_cross_identity_idor.py else 分支＋工具層接線） |
| E04 | 0047 | 🔴 HIGH | ③ | 身分固定：operation tool 消費的身分永遠固定 DispatchAgent 不由 caller 衍生，無測 | 🏁 ADR-0172（test_dispatch_identity_fixation.py spy 攔截消費身分） |
| E05 | 0034 | 🟡 MED | ④ | GRANDFATHER 清單「永久空」長度靜態斷言從未實作 | 🏁 ADR-0170（main() 空清單斷言＋meta-test） |
| E06 | 0035 | 🟡 MED | ④ | POST `/system/sliding_window` admin-only 無測（guard 只查認證存在非等級） | 🏁 ADR-0170（ADMIN_REQUIRED 雙向同步＋403 行為測試） |
| E07 | 0053 | 🟡 MED | ④ | setup/embedding 端點 admin-only 無測（0035 翻版） | 🏁 ADR-0170（同 E06 一族） |
| E08 | 0032 | 🟡 MED | ④ | Guard #21 forbidden 契約漏 `src.handlers`／`src.scripts` | ⬜ |
| E09 | 0004 | 🟡 MED | ① | ruff banned-api 應作 CI 閘掃全庫，但 ruff 完全不在 CI/guard_all | 🏁 ADR-0171（Guard #44 TID251 納閘） |
| E10 | 0028 | 🟡 MED | ⑤ | bcrypt SHA256 prehash（防 >72 byte 密碼碰撞）無真行為測試 | ⬜ |
| E11 | 0018 | 🟡 MED | — | refresh token cookie `httponly=True` 無測/守衛 | ⬜ |
| E12 | 0019 | 🟡 MED | — | login/logout/refresh 的 refresh cookie `path="/"` 一致性無測 | ⬜ |
| E13 | 0019 | 🟡 MED | — | 前端 `fetchTouka`/refresh `credentials:'include'` 無測 | ⬜ |
| E14 | 0005 | 🟡 MED | — | vault schema↔下沉 impl action enum parity 無測 | ⬜ |
| E15 | 0006 | 🟡 MED | — | 測試沙盒 env 重導（MEMORY_APP_DATA_DIR 等）無斷言指向 temp | ⬜ |
| E16 | 0006 | 🟡 MED | — | 無守衛阻 ToukaProxy/tests 等子專案 tests/ 重生（root testpaths 不收攏＝CI 靜默排除） | ⬜ |
| E17 | 0007 | 🟡 MED | — | 自癒不覆蓋既有 config：唯一測試用 skills.json（不在 ESSENTIAL_CONFIGS）＝假綠 | 🏁 ADR-0169（改真檔 models.json＋example 同場） |
| E18 | 0001 | 🟡 MED | ⑧ | heartbeat `awaken_url` 跨服務契約無測（route rename→404 靜默停脈） | ⬜ |
| E19 | 0021 | 🟡 MED | ③ | terminal 用 call_kwargs 傳入的 auth_level 決策（無硬編預設）無測 | 🏁 ADR-0180 |
| E20 | 0022 | 🟡 MED | ③ | vault_tools DI 注入 `resource_proxy`（vault_tools.py:61）無測 | 🏁 ADR-0183 |
| E21 | 0033 | 🟡 MED | ③ | `/editor/files` 路由 `is_relative_to` 遍歷防護無測 | 🏁 ADR-0179 |
| E22 | 0033 | 🟡 MED | ③ | model-provider fetchers `follow_redirects=False`（SSRF）3 守衛皆可繞 | 🏁 ADR-0179 |
| E23 | 0038 | 🟡 MED | ③ | touka-init `chown/chmod` config/data/named volumes 無測（dev compose 無守衛） | 🏁 ADR-0185 |
| E24 | 0039 | 🟡 MED | ③ | proxy.py else 分支 Guest fallback（絕不讀 X-Agent-ID）無測 | 🏁 ADR-0180 |
| E25 | 0042 | 🟡 MED | ③ | `TreeHistory.from_flat_list` 遷移邏輯無測（測試 payload 全空 list） | 🏁 ADR-0183 |
| E26 | 0045 | 🟡 MED | ③ | GET `/ui/mission` 須 `model_dump()`（否則 raw model→500） | 🏁 ADR-0181 |
| E27 | 0046 | 🟡 MED | ③④ | dispatch 身分非特權（is_privileged()→False）僅源碼字串測 | 🏁 ADR-0180 |
| E28 | 0049 | 🟡 MED | ⑦ | AsyncFileLock 跨行程互斥無測（in-process asyncio.Lock 遮蔽） | 🏁 ADR-0182 |
| E29 | 0051 | 🟡 MED | ③ | ToukaDispatch callback trigger（DONE/FAILED→回呼 proxy）零 pytest 覆蓋 | 🏁 ADR-0183 |
| E30 | 0059 | 🟡 MED | ③ | 前端 `fetchChatStream` 建構 `x-touka-skills` header 只字串檢查非執行 | 🏁 ADR-0184 |
| E31 | 0061 | 🟡 MED | ③ | dispatcher 模式 RBAC 可見性 deny 路徑（tool.py:192）無測 | 🏁 ADR-0180 |
| E32 | 0061 | 🟡 MED | ③ | dispatcher bounded-union 排除半（授權但不在兩集合須隱藏）無測 | 🏁 ADR-0180 |
| E33 | 0062 | 🟡 MED | ⑧ | POST `/ui/sessions` `active_skills` 轉發無測（姊妹 auth_level 有測） | ⬜ |
| E34 | 0064 | 🟡 MED | ⑧ | `dispatch_get_board` 讀路徑板完整性無測（WRITE 閘有測 READ 鏡像無） | ⬜ |

---

## 批 A — HIGH（4 條，最高優先）

### E01 · auth-level 降級守衛無測（ADR-0021，🔴 HIGH，家族③）
- **不變式**：MonitorLayer 的 Auth-Level-Change Guard（`monitor.py:95-111`）——非 admin-authority／非 target owner 發起 SafeAuto/FullYOLO 時，須強制降級 `auth_level="Manual"`（防提權）。
- **失效模式**：刪除降級賦值（line 111）／設 `is_authorized=True`／從檢查集合移除 SafeAuto → 非 admin 非 owner 的人類可把 session 升到 FullYOLO，取得無人值守自動執行危險工具。
- **現況執法**：❌ 無真執法。**唯一測試 `test_monitor_authlevel_before_rebind.py`（即上一輪建的 G16／ADR-0163）是源碼序字串鎖**——只斷言 `'Unauthorized auth_level'` 字串先於 `_maybe_assume_soul_identity`，**從不呼叫 `process()`**，授權決策與降級賦值零執行（tree-wide grep 證實無任何測試呼叫 `MonitorLayer.process()`）。審計 log 亦只 `logger.warning`、無 audit_service、無守衛。
- **建議方向**：新增行為測試，真呼叫 `MonitorLayer.process()`，餵（非 admin 非 owner identity + FullYOLO 請求）斷言結果 `auth_level` 被降為 Manual；再餵（admin 或 owner）斷言不降。**這是「G16 補課」——原守衛只鎖結構、需升為行為級。**

### E02 · path-escape 沙盒只在 smoke（ADR-0022，🔴 HIGH，家族②）
- **不變式**：vault-tools 每個檔案操作前必先過 `_enforce_root_bound`（`core_file_ops.py:21-60`）——禁 `../` 遍歷、絕對路徑注入、跨磁碟逃逸。此為唯一 path-escape 防線（`ResourceManager.read_file_text` 是 raw `Path().read_text` 無沙盒）。
- **失效模式**：削弱 `is_relative_to` 逃逸檢查（`core_file_ops.py:53`）→ vault 外任意檔案讀寫，**過全部自動閘（pytest + guard_all）零告警**。
- **現況執法**：❌ 無。唯一 exercise 是 `tests/smoke/smoke_io_prison.py`（函式 `run_io_prison_smoke_test`），`pytest.ini python_files=test_*.py/python_functions=test_*` **從不收集**；guard_all 不引；CI 不跑。Guard #1 lint_ssot 反而把 core_file_ops.py **列白名單**（raw-IO 豁免、非執法）。
- **建議方向**：把 `smoke_io_prison.py` 的關鍵斷言升格為 `test_io_prison.py`（pytest 可收集），或納入 guard_all 執行 smoke。屬**家族②治本**：全面盤點 `tests/smoke/*.py` 中承載安全不變式者，逐一升格/納閘。

### E03 · mission board 跨身分 IDOR else 分支無測（ADR-0044，🔴 HIGH，家族③）
- **不變式**：MissionManager/工具層任何對「非 self、非 DispatchAgent」的跨身分板存取須以 `PermissionError` 拒絕（`mission_manager.py:54`，`_resolve_target_agent_id` 的 else 分支）。這是工具路徑存取控制的**唯一閘**（`mission_tools.py` 無過濾轉發 target_agent_id）。
- **失效模式**：把 line 54 改為 `return target_agent_id` → 跨 agent 讀+寫任務板 IDOR。**變異證明：改後全 32 mission 相關測試綠。**
- **現況執法**：❌ 無。所有 target_agent_id 測試用 "DispatchAgent"（line 53）或 self；`test_ui_mission_idor_blocked` 測的是 router 自己的閘（`ui_sessions.py:782-783`）呼叫 `get_current_mission` 無 target_agent_id、從不到 line 54。
- **建議方向**：新增測試——以 caller identity A 對 target_agent_id=B（非 self、非 DispatchAgent）呼叫工具層 `get_board`/`update_task`，斷言 `PermissionError`。

### E04 · 身分固定（operation-tool 消費點）無測（ADR-0047，🔴 HIGH，家族③）
- **不變式**：operation tools 交給 MissionManager 做授權 AND 板路徑構造的身分物件，**永遠是固定非特權 DispatchAgent、絕不由 caller 提供的輸入衍生**（Vuln 1 身分偽冒／Vuln 2 路徑遍歷的核心）。
- **失效模式**：若 operation tool 改由 caller 提供的欄位取身分 → 身分偽冒（越過 DispatchAgent 邊界）。
- **現況執法**：❌ 部分假綠。握手/session token/過期/handshake-required 都有真執法（見 `early_c4.md` 0047 summary），**唯獨「operation tools 實際消費的身分」這條核心無行為測試**斷言它必為固定 DispatchAgent 而非 caller 衍生。
- **建議方向**：新增測試——以夾帶偽 identity 欄位的請求呼叫 operation tool，斷言授權與板路徑仍用固定 DispatchAgent（偽欄位被忽略）。與 E03 綁做（同 ToukaDispatch 身分家族）。

---

## 批 B — 端點認證等級家族（家族④，一支守衛可關整族）

> **E05/E06/E07/E08 共享同一治本**：`Guard #22/#24`（endpoint-auth）與 `_AUTH_KEYWORDS` 只驗「有認證 Depends」不驗「等級/範圍」。建議**一次性強化該守衛辨識 `require_admin` vs `get_verified_identity`**（以路由宣告的 admin 端點清單為 SSOT），即可同時關掉 E05–E07；E08 為 Guard #21 的類比範圍缺。

### E05 · GRANDFATHER 長度靜態斷言從未實作（ADR-0034，🟡 MED）
- **不變式**：auth-guard 的技術債逃生口 GRANDFATHER 清單須「清零後永久維持空」，以「清單長度靜態斷言」執法，使其不能被重用來靜默豁免狀態變更端點的認證。
- **失效模式**：`guard_endpoint_auth.py` 把 GRANDFATHER OR-fold 進 `_is_allowlisted()` 且 `main()` 不檢其長度 → 往清單加任何 dict 即靜默豁免某端點認證。`test_guard_endpoint_auth.py` 只斷言 ALLOWLIST 非空（錯清單、錯方向）。
- **現況執法**：❌ 無。承諾的「清單長度靜態斷言」從未落地。
- **建議方向**：加 `assert len(GRANDFATHER)==0`（或掃描斷言）。

### E06 · sliding_window admin-only 無測（ADR-0035，🟡 MED）
- **不變式**：POST `/v1/system/sliding_window` 須 admin-only（router `Depends(_require_admin)`）；僅 admin 可改系統級滑窗設定。
- **失效模式**：`_require_admin`→`_require_authenticated` 降級全綠——Guard #22 `_AUTH_KEYWORDS` 子串 `_require_` **同時匹配兩者**；無任何非 admin POST→403 測試。
- **現況執法**：❌ 無等級執法。
- **建議方向**：加非 admin POST→403 測試；併入家族④守衛強化。

### E07 · setup/embedding 端點 admin-only 無測（ADR-0053，🟡 MED）
- **不變式**：五個 setup/embedding 端點須 admin-only，尤其 POST `/embedding/switch`（改持久化 EMBEDDING_MODEL + 重置向量單例）與 GET `/download-status`。
- **失效模式**：**變異證明**——把 `require_admin`→`get_verified_identity`（setup.py:118/127），**全 43 `test_setup_endpoints` + Guard #22 綠**。任何認證非 admin 可改設定。
- **現況執法**：❌ 無等級執法（0035 翻版）。
- **建議方向**：同 E06；家族④守衛強化一次涵蓋。

### E08 · Guard #21 forbidden 契約漏子包（ADR-0032，🟡 MED）
- **不變式**：`memory_server`（ToukaMemory）不得 import 任何 ToukaProxy `src.*` 內部子系統（ADR 定義禁集為「其餘 src internals」）。
- **失效模式**：`.importlinter` forbidden 契約只列 9 子包，**漏 `src.handlers`／`src.scripts`**。**變異證明**：memory_server import `src.handlers`+`src.scripts` 契約 KEPT/exit 0，控制組 `src.services` exit 1。直接 `memory_server→src.handlers`（正重構的 ReAct core）靜默過。
- **現況執法**：⚠️ 部分（9 子包有、其餘無）。
- **建議方向**：把 forbidden 契約改為「禁 `src` 下除白名單注入邊界外的全部」（或補齊 handlers/scripts），使範圍＝ADR 全稱。

---

## 批 C — 認證/會話與基礎建設 MED

### E09 · ruff 未作 CI 閘（ADR-0004，🟡 MED，家族①）
- **不變式**：ruff banned-api 應作 CI 閘對整個代碼庫執行（路徑函式禁令 os.getcwd/abspath/realpath、`sys.path.insert`、`src.config.get_app_data_dir` 收束禁令等）。
- **失效模式**：掃描當時 ruff 完全不在 guard_all、也不在當時的 release workflow（只 docker/trivy/boot，已依 ADR-0238 刪除）；唯一 ruff 測試只對合成檔驗 TID251 機制。刪任何 banned-api 條目零守衛變紅。
- **現況執法**：❌ 無。
- **建議方向**：**家族①治本**——新增一道 guard_all 守衛跑 `ruff check`（或 `--select` banned-api）對真實 src/ 全量掃描，非零退出即 fail。一次啟用全部 ruff 半邊禁令（同時涵蓋 0017 sys.path.insert、0056 as-any 若納規則）。

### E10 · bcrypt SHA256 prehash 無真行為測試（ADR-0028，🟡 MED，家族⑤）
- **不變式**：唯一授權 bcrypt caller（auth_service.py）須保 bcrypt 前的 SHA256 預雜湊，使兩個僅在第 72 byte 之後相異的密碼不會同時驗證通過。
- **失效模式**：refactor 回 `bcrypt.hashpw(password.encode(), salt)` → 重引 >72-byte auth bypass。**變異證明**：拔掉 prehash，唯一行為測試 `test_long_password_handling` 兩斷言仍過（實證假綠）；驗證者另構 `'A'*72+'DIFFERENT'` 對 `hash('A'*100)`＝vuln 版驗真、正確版驗假。
- **現況執法**：❌ 無（AST import ban 對白名單 auth_service.py early-return 不執行雜湊）。
- **建議方向**：加碰撞對測試——斷言共享 72-byte 前綴但之後相異的兩密碼不會互相驗證通過。

### E11 · refresh cookie httponly（ADR-0018，🟡 MED）
- **不變式**：refresh token 僅經 HttpOnly cookie 遞送（`auth.py:114-119` `httponly=True`），防 XSS 竊取。
- **失效模式**：`httponly` 翻 False → refresh token 暴露於 XSS，靜默回歸。
- **現況執法**：❌ 無（H1 測試只驗 secure+rotation 不驗 httponly；grep httponly 於 tests 零命中）。
- **建議方向**：加測試讀 Set-Cookie 斷言 `HttpOnly` flag。

### E12 · refresh cookie path 一致性（ADR-0019，🟡 MED）
- **不變式**：login/logout/refresh 三處 refresh_token cookie 須一致 `path="/"`（logout `delete_cookie` 的 path 與 login/refresh `set_cookie` 相符），否則登出清不掉 cookie→session 存活可自動重登。
- **失效模式**：logout 的 path 字面量與 set 不符 → 登出無效。生產目前正確但無鎖。
- **現況執法**：❌ 無（H1 測試只斷言 secure、test_auth_routers 只驗 cookie 存在）。
- **建議方向**：加測試斷言三處 Set-Cookie/delete_cookie 的 path 皆 `/`。

### E13 · 前端 credentials:'include'（ADR-0019，🟡 MED）
- **不變式**：`fetchTouka`（connections.ts:278）與 `refreshAccessToken`（:99）須 `credentials:'include'`，跨來源 API 呼叫才能送/銷毀 HttpOnly refresh cookie。
- **失效模式**：移除→跨來源部署 refresh/logout cookie 遞送靜默失效、全綠。
- **現況執法**：❌ 無（無 vitest 斷言此 flag；guard_fetch 只保證經 fetchTouka 路由不驗 flag 值）。
- **建議方向**：加 vitest 斷言 fetch options 含 `credentials:'include'`。

### E14 · vault schema↔impl parity（ADR-0005，🟡 MED）
- **不變式**：Proxy 廣告的 vault action enum/條件必填 不得與下沉 ToukaMemory 實作實際接受者漂移（無影子定義）。
- **失效模式**：廣告一個 impl 已不支援的 action（模型調用→靜默失敗）或 impl 新增未廣告 → 無執法捕捉。
- **現況執法**：❌ 無（`test_tool_schema_hygiene` 只靜態對釘廣告端 shape 的第二份影子副本，非跨層 parity；VAULT_ACTIONS 模組外零 import）。
- **建議方向**：加跨層 parity 測試——比對廣告 action enum 對上 `memory_server.tools.vault_tools` 實際接受的動作/簽名。

### E15 · 測試沙盒 env 重導無斷言（ADR-0006，🟡 MED，家族②鄰）
- **不變式**：全域沙盒須在匯入任何 `src.*` 前把 MEMORY_APP_DATA_DIR/SANCTUARY_DATA_PATH/TOUKA_CONFIG_DIR 重導至臨時目錄（`conftest.py:31-44`，早於 line 51 `from src.main import app`），使測試永不讀寫實體 data/config。
- **失效模式**：刪除/上移重導區塊或提前某 src import → 測試靜默改跑實體目錄、仍全綠。
- **現況執法**：❌ 無（8+ 消費檔只 USE fixture，無一斷言 data 目錄落 temp；lint_ssot 對 conftest.py 豁免 ENV 規則）。
- **建議方向**：加 canary 測試——斷言 `get_app_data_dir()`/settings 路徑 `is_relative_to` 臨時根。

### E16 · 子專案 tests/ 重生無守衛（ADR-0006，🟡 MED）
- **不變式**：子專案內部 tests/ 已廢除，`pytest tests/`（root testpaths=tests）單一入口收攏全套。
- **失效模式**：ToukaProxy/tests 或 ToukaMemory/tests 重生 → root testpaths 不收攏→該批測試 CI 靜默排除＝假綠。觸發點已存在（`ToukaProxy/pytest.ini:4` 仍 `testpaths=tests`）。
- **現況執法**：❌ 無（Guard #31 只限前端）。
- **建議方向**：加守衛——glob 斷言子專案下無 `test_*.py`（或 CI 收集數對帳）。

### E17 · 自癒不覆蓋既有 config 假綠（ADR-0007，🟡 MED，與前作 G18 同源）
- **不變式**：自癒絕不覆蓋已存在的 config/.env（`ensure_configs` 的 `if target_path.exists(): continue`），保護使用者手改。
- **失效模式**：保護分支從未被真驅動＝假綠；有人改壞覆蓋邏輯不會變紅。
- **現況執法**：❌ 無真執法（唯一測試 `test_ensure_configs_does_not_overwrite` 用 `skills.json`——**不在 `ESSENTIAL_CONFIGS`**，保護分支從未進；真驅動者 `smoke_soul_recovery.py` 是 smoke 不收集）。
- **建議方向**：改用 ESSENTIAL_CONFIGS 內的真檔驅動保護分支並斷言未覆蓋；併家族②smoke 升格。

---

## 批 D — 家族③（源碼序/生產路徑無測）其餘 MED

> 這些共通特徵：被保護的那一行變異後測試仍全綠；治本＝各補一條驅動真路徑的行為/端到端測試。逐條詳見 scratchpad；此處列不變式與建議。

- **E18 · ADR-0001 heartbeat awaken_url**：`ToukaHeartbeat/config.py:50-52` 硬編 route 字串與 Proxy 註冊路由無單一真源連結；proxy 端測試 mock 掉 `trigger_heartbeat`。→ 加跨服務契約測試（或 openapi route SSOT 對釘 client 字串）。
- **E19 · ADR-0021 terminal auth_level**：`terminal.py:489 kwargs.get("auth_level","Manual")` 無測驅動非 Manual 過真 executor→terminal 路徑。→ 加測試餵 SafeAuto/FullYOLO 斷言 terminal 決策反映之。**🏁 ADR-0180（批⑤-B）**：spy `_classify_command_strategy` 捕 auth_level、execute_command 餵 FullYOLO/SafeAuto/預設→序列斷言；變異硬編 Manual → 紅。
- **E20 · ADR-0022 DI 注入 resource_proxy**：`vault_tools.py:61` 唯一注入點，dispatcher 無 fallback；唯一 bridge 測 mock 掉 impl 只斷言 agent_id。→ 加測試斷言 dispatch payload 含 `resource_proxy`。
- **E21 · ADR-0033 editor 路由遍歷**：`editor.py:46-51` `is_relative_to` 無測；還原為 `str.startswith` 重引兄弟目錄繞過全綠。→ 加 `/editor/files` 帶 `../` payload→拒絕測試。**🏁 ADR-0179（批⑤-A）**：TestClient 驅動端點斷言 `../` 夾制回 cwd（父目錄哨兵不可見）；變異 `if False` → 父目錄外洩 → 紅。
- **E22 · ADR-0033 fetcher SSRF**：3 守衛皆可繞（inspect.getsource 字串鎖／AST 只查存在非 value=False／scoped 只 gemini）；grok fetcher 回 `follow_redirects=True`（帶空白）三者全繞。→ 加驅動真 client 構造斷言 value=False 的行為測試。**🏁 ADR-0179（批⑤-A）**：capture-kwargs parametrized 全 6 fetcher 斷言 `follow_redirects is False`＋MockTransport 302→metadata 不跟隨（威脅級）；變異 openai→True → 2 測紅。
- **E23 · ADR-0038 touka-init chown/chmod**——🏁 已釘死（ADR-0185，owner 拍板 (a) CI 所有權斷言）：boot 閘實為雙漏網（GH runner uid 恰 1001＝bind mount 意外可寫／named volume 開機不寫）。已補 gate `exec -T -u 1001` 寫入探針（proxy 三掛載+heartbeat 設定目錄）＋dev/ghcr chown 涵蓋面與 workflow 接線三支靜態 pin（chown 段綁定防 chmod 行遮蔽）。其中 gate 寫入探針與 workflow 接線隨 release workflow 依 ADR-0238 刪除；chown 涵蓋面仍由 compose／Dockerfile 靜態測試鎖住。
- **E24 · ADR-0039 proxy Guest fallback**：`proxy.py:68-70/106-108` else 分支無測；唯一 Guest 測試只釘 pure helper `_build_guest_context`；改成讀 header 即提權全綠。→ 加端點測試：無認證+帶 X-Agent-ID→斷言解析為 Guest 且忽略 header。**🏁 ADR-0180（批⑤-B）**：TestClient spy process_proxy_request 斷言 ctx Guest/identity None/caps=[]；變異 guest 讀 X-Agent-ID → 紅。
- **E25 · ADR-0042 from_flat_list**：`ui_sessions.py:200` 呼叫，但所有 collected 測試 payload 皆空 list（不進迴圈）；刪 childrenIds append 全綠。→ 加非空 legacy flat-list→斷言 parentId 鏈/childrenIds/currentId。
- **E26 · ADR-0045 /ui/mission model_dump()**——🏁 已釘死（ADR-0181）：`ui_sessions.py:801` 無測（測試都停 404/403 不 seed 真 mission）；`return mission` 全 11 綠→有 active mission 時 500。已補 stub 回真 MissionState→GET 斷言 200 且回 dict（變異 `return mission` 撞 `dict[str, Any]` 回應驗證→500→紅）。
- **E27 · ADR-0046 dispatch 非特權**（家族④亦）：僅源碼字串測；注入 `is_privileged()→True`/`capabilities=['can_supervise_dispatch']` 過字串測+全 own-board round-trip。→ 加測試斷言 `_get_dispatch_identity()` 回傳 `is_privileged()==False`、capabilities==[]。**🏁 ADR-0180（批⑤-B）**：實際構造點為 `ui_sessions.py:788-790`（非獨立函式）；spy MissionManager.get_current_mission 捕 target_identity 斷言 DispatchAgent/非特權/caps=[]；變異 agent_role base→Owner → 紅。
- **E29 · ADR-0051 dispatch callback trigger**：`server.py:400-414` DONE/FAILED→`_notify_touka_proxy` 零 pytest 覆蓋（終態分支從不進、DONE 測試都繞過 ToukaDispatch）。→ 加測試以終態 status 驅動 `dispatch_update_task` 斷言 POST 觸發（token/URL/header）。
- **E30 · ADR-0059 x-touka-skills header 建構**——🏁 已釘死（ADR-0184）：後端消費側全真執法，漏前端 `fetchChatStream` header **建構**（只字串檢查）。已補 vitest（真 fetchChatStream＋stub fetch）：非空→逗號串接、空/缺席→不送（變異刪 :379 紅）。
- **E31 · ADR-0061 dispatcher RBAC 可見性 deny**：`tool.py:192` 移 `is_authorized and` 全 208 測試綠。→ 加 dispatcher 模式 + is_mcp_server_allowed=False→斷言工具隱藏。**🏁 ADR-0180（批⑤-B）**：server 在 union 內+RBAC deny→隱藏；變異 `True and`（丟 is_authorized 運算元）→ 只 E31 紅、E32 綠。
- **E32 · ADR-0061 bounded-union 排除半**：`tool.py:191-195` 塌成 `if is_authorized: show=True` 18/0 綠。→ 加授權但不在 active∪skill_required 的 server→斷言隱藏。**🏁 ADR-0180（批⑤-B）**：授權但 union 外→隱藏；變異塌成 `if is_authorized`（丟 union 運算元）→ 只 E32 紅、E31 綠（兩測互證非重疊）。

---

## 批 E — 家族⑦⑧ MED

- **E28 · ADR-0049 AsyncFileLock 跨行程互斥（家族⑦）**——🏁 已釘死（ADR-0182）：no-op file lock 兩併發測試仍過（in-process asyncio.Lock 自己序列化遮蔽）。已補**真跨行程** subprocess 測試（子行程持同步 FileLock→本行程 MissionLock 斷言 Timeout＋殺子後陽性對照；變異 no-op acquire 紅）。
- **E33 · ADR-0062 active_skills 轉發（家族⑧）**：`ui_sessions.py:355` 刪除全 12 測試綠→None→merge/[] 靜默丟技能持久化；**姊妹 auth_level(line356) 有真 TestClient router→DB 測試、active_skills 獨缺**。→ 補對稱測試：POST /ui/sessions 帶 active_skills→讀回斷言持久化。
- **E34 · ADR-0064 dispatch_get_board 讀路徑（家族⑧）**：`server.py:442-451` 刪除→被取代 mission 板洩漏給 stale 外部 session、全綠；**WRITE 路徑(353-360)有自己被測的閘，READ 鏡像閘無測**。→ 補對稱測試：外部 session bound_mission_id 不符現行→`get_board` 拒絕。

---

## LOW 面值（~57，低優先，多需主動改壞/dev-only）

> 這些多屬「需主動改壞才觸發」「dev/顯示層」「防禦縱深冗餘」「有下游 backstop」。列入待辦但優先序最低；完整證據見 scratchpad。**其中一組可隨家族治本一併補**：

- **compare_digest 計時安全三連（0046/0047/0050）**：`ToukaDispatch/server.py:67` `_verify_dispatch_token` 用 compare_digest 但無測，改 `==` 過 100% 套件；兩姊妹站（auth_service/heartbeat）各有源碼字串守衛、dispatch server 獨缺→**一支源碼字串測試補齊三處**。
- **0049×5（filelock 家族）**——🏁 全數收口（ADR-0182）：release 無測／`_file_locks` 快取物件同一性無測／`_reset` 只驗 `_asyncio_locks` 漏 `_file_locks`／30s finite timeout 無測（四條補測＋各自變異紅）；**`FileLockAsyncRule` 盲點（漏 `import filelock; filelock.FileLock()`）＝守衛補 `check_Call` 攔截**（TDD 向先紅後綠、真樹預檢零既有違規）。
- **0053×3**：change_own_password 清 is_temp_password 無測（自助改密後橫幅永掛）／embedding switch 不觸發 re-embed 負向不變式無測／**bootstrap admin 密碼須隨機非硬編無測**（改固定字串仍過所有測試；開源=源碼可見預設 admin 密碼提權，緩解 is_temp_password 橫幅）。
- **dev-tooling 群（0054/0055/0056/0057/0058）**：tsconfig.guard include 涵蓋／npm test 不縮範圍／`as any` 禁令零守衛且 eslint 主動矛盾（家族①）／mock 漂移偵測只 filter 9 硬編檔名／pool:'forks'+maxWorkers 只驗子串／model_copy 源碼字串鎖／PipelineContext 型別註解無 type checker。多有 Docker build/手動 npm test backstop。
- **其餘（0004 pathfuncs／0007 autotrigger／0008 CORS expose_headers／0016×3 依賴守衛自測／0017 sys.path.insert〔家族①〕／0020×2 soul prune+config 合併／0023 noqa 精確／0042 currentId 缺失 raise+cyclic 回退／0026 localStorage alias/bracket／0027 TypeError 成員／0030 sqlite3 basename 白名單／0031×2 console runner glob+註冊／0033 sanitize/SQL IN／0035 is_home_mode 衍生／0062 View confirmToast gate／0064 clear_mission_tokens 輪替）**：逐條見 scratchpad。

---

## 附錄 A — 推翻的假陽性（4，不需建守衛）

| ADR | 原聲稱 | 推翻理由 |
| :-- | :-- | :-- |
| 0010 | ToolExecutor 身分閘 fail-closed 無測（原 high） | `test_tool_errors_structured.py:170` 真驅動 `execute_to_queue` ctx identity=None、斷言 error_class==internal_error＝真打閘 293-312（analyzer 只搜訊息字串漏掉） |
| 0011 | AnthropicRequest.stream 欄位無執法 | **實證推翻**：移除欄位→`test_react_integration::test_full_react_loop_with_tool` 真變紅（content 被清空）；analyzer「移除仍綠」事實錯誤 |
| 0023 | parse_noqa_comments 正則無測 | Guard #1 跑 lint_ssot 全樹→run_guard→parse_noqa_comments；~30+ 真實 noqa 承載中，正則壞→那些豁免變違規→#1 紅＝真執法 |
| 0024 | Fetch SSOT 守衛註冊無測 | `guard_fetch.ts` fast-glob 掃真實 src＋已註冊 guard_all #11＝核心不變式有執法；缺的只是「守衛的守衛」註冊 meta-test（43 道中僅 4 道有）＝防退化 meta 洞、非執法缺席 |

---

## 附錄 B — ADR 對齊線線索（過時 ADR）

- **ADR-0063（persona Skill Hub）已被 ADR-0077（2026-06-07）完全取代**：子系統整個刪除——glob `skill_hub_service.py`/`test_skill_hub_service.py` 零檔、grep `skill_hub|SkillHub|HubSkill|sync_skills|ALLOWED_SYNC` 零命中（只在 docs/adr、site 生成 HTML、SECURITY.md 散文）。其四不變式全 moot。→ **ADR 對齊/過時回顧線的首個確認標的**（狀態欄若未標 superseded 應補）。

---

## 塊7 追加（ADR 0065–0071，2026-07-22）

> 續掃第一塊（workflow `early-guard-coverage-scan`，analyze=sonnet ×7 → verify=opus ×3）。本塊為新期基礎建設 ADR（dispatch/skill/creation/guard 本體），整飭度高、低產但乾淨：**37 條不變式中 34 REAL、3 條 MED 確認缺口、~6 LOW**。原始資料 scratchpad `early_c7.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E35 | 0066 | 🟡 MED | ⑧④ | **⚠️ LIVE 缺口**：`delete_skill` 讀 `fm.get("is_system")`（頂層）但生產寫入路徑把 is_system 收進巢狀 `metadata:`（其餘 5 讀取點皆用 `read_touka_field`）→ 真實系統技能保護失效可被刪；測試用扁平格式＝假綠 | 🏁 ADR-0175 |
| E36 | 0070 | 🟡 MED | ④⑥ | `embedding_service.apply_switch()`「絕不自動 re-embed／絕不動 FactualStore」無負向斷言——尾端加 `reembed_agent()` 全綠 | ⬜ |
| E37 | 0071 | 🟡 MED | ⑤ | Guard #30 run_all 聚合/收集分支（Check 1/2/3）僅由「乾淨庫 run_all()==[]」間接驗證——把比對改恆一致（line 294 `bad={}`）全綠、版本漂移永遠靜默放行 | ⬜ |

### E35 · delete_skill 系統技能保護讀錯鍵路徑（ADR-0066，🟡 MED〔verify 由 HIGH 降〕）——🏁 已修（ADR-0175）
- **⚠️ 非僅測試假綠——LIVE 生產缺口**：`skill_service.py:337-338` `delete_skill` 用 `fm.get("is_system")`（頂層鍵）；但 `build_frontmatter`（skill_parser.py:87+）把 is_system 收進巢狀 `metadata:`（過 Anthropic quick_validate），其餘 5 讀取點全走 `read_touka_field`（metadata 優先）。真實落地檔 system-skill-dispatcher / system-mcp-dispatcher / deep-research-coordinator SKILL.md 皆巢狀 → `fm.get("is_system")`==None → 保護虛設 → 真實系統技能可刪（verify 實跑佐證）。
- **假綠**：`test_delete_system_skill_protected`（:204）fixture 用扁平 `["name: sys-skill", "is_system: true"]`→ `fm.get` 恰命中→過。
- **降級理由（HIGH→MED）**：DELETE 端點掛 `_require_skills_role`=is_privileged（Owner/Debugger/admin）＝非提權/IDOR/秘密；非 LLM 工具；刪後 sync_skill_dispatcher 自動重生 dispatcher。**但 deep-research-coordinator 等 is_system 內容技能會被永久刪。**
- **治本**：生產 skill_service.py:338 改 `read_touka_field(fm, "is_system")`（對齊其餘 5 讀取點）；測試 fixture:204 改巢狀格式釘死。

### E36 · apply_switch 負向不變式無斷言（ADR-0070，🟡 MED）
- **不變式**：線上 `apply_switch()`（embedding_service.py:162-174）絕不自動觸發 reembed、絕不寫 FactualStore（re-embed 離線限定，docstring 明文）。
- **假綠**：`test_embedding_service.py:116-128` 只斷言三個有意副作用（env 寫入/apply_runtime_env/_reset ×1），從不斷言 reembed 未呼叫、FactualStore 未寫；setup 端點測試整個 mock 掉 apply_switch；guard 零命中。變異：尾端加 `reembed_agent(target)` 全綠。
- **嚴重度**：MED——跨模組資料安全契約（線上切換不得觸發破壞性離線重嵌入使用者記憶）；現靠 docstring 自律無 backstop、生產乾淨＝防迴歸。
- **釘死**：加測 monkeypatch spy 到 reembed 入口＋FactualStore 寫入，`assert_not_called()`。

### E37 · Guard #30 run_all 收集分支僅乾淨庫間接驗證（ADR-0071，🟡 MED，家族⑤新樣態）
- **不變式**：Guard #30 Check 2 = Python 版本在 .python-version / pyproject / Dockerfile / ruff.toml 間完全一致。
- **假綠**：抽取純函式（py_from_dockerfile 等）各有合成不一致測試，但 run_all 內「比對 canonical→收集 bad→append [PYVER]」（guard_environment_parity.py:293-296）唯一涵蓋＝現況乾淨庫 `run_all()==[]`。變異 line 294 `bad={}`→乾淨庫仍 []、全綠→對真實漂移永遠靜默。**同型盲區 Check 1（uv.lock diff 收集）、Check 3（>= 釘版收集）皆然。**
- **嚴重度**：MED——跨服務版本一致性契約；下游 backstop＝Docker `uv sync --locked`。
- **釘死**：monkeypatch _read 讓某 Dockerfile 回 `FROM python:3.11-slim`（.python-version=3.12）驅動 run_all()，斷言 `any('[PYVER]' in e ...)`；Check 1/3 同理補真實漂移情境。

### 塊7 LOW 面值（~6）
- **L-0065（純文件）**：Skill 2 外部指南（兩份重複 md）與 server.py 工具簽章無 parity 測試；改回傳前綴字串全綠、僅外部 AI 解析失敗。
- **L-0067（OS 層）**：dispatch_hooks.json 應限 Owner 可寫但程式零存取控制/啟動權限檢查；需先取得 FS 寫入權。建議加啟動 stat 檢查。
- **L-0069a（③）**：MCP commit 後 `sync_system_dispatcher()`（mcp_kind.py:57）呼叫端無斷言；可用性迴歸非安全。
- **L-0069b（②衛生非缺口）**：smoke_creation_engine.py 死測試（不被收集）但等效涵蓋已存在於 test_creation_route/test_creation_scaffold＝可清理非假綠。
- **L-0070（軟註記）**：FactualStore re-embed 唯讀不變式無明確 `assert not factual.save_fact.called`（MagicMock 靜默吞意外寫入）；建議斷言唯讀方法集合。
- **L-0071（⑤，E37 之 LOW 變體）**：Guard #30 Check 3 釘版 run_all 收集分支同 E37 盲區；LOW 因需手動改壞＋Docker backstop。

### 塊7 值得記的 REAL（涵蓋充分）
- **ADR-0068 自證**：guard_adr_integrity（Guard #28）五檢查全 REAL，含 **R4 新建的 ADR-0173 第 4 檢查（修訂雙向一致）meta-test 強、6 方向判別＋紅→綠成對**。
- **ADR-0065/0067**：ToukaDispatch 認證/token/env 隔離/auto-invoke 共 12 條全真驅動。
- **ADR-0069**：創建引擎 8 條（403 真 TestClient、scaffold 注入 ast.walk、Guard #29 主權）全 REAL。

---

## 塊8 追加（ADR 0073–0079，2026-07-22；0072 已退役跳過）

> 續掃第二塊。**1 HIGH + 3 MED 確認（E38–E41）＋~5 LOW＋4 降級為 LOW**；⭐E39 是首見自批 A 以來的 HIGH（auth-level 提權家族③，0021 翻版）。REAL 涵蓋強（0074 catalog 安裝安全 8 條 / 0077 退役 / 0078 韌性群）。原始資料 scratchpad `early_c8.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E39 | 0078 | 🔴 HIGH | ③ | POST /v1/heartbeat/retrigger 強制 `ctx.auth_level="SafeAuto"`（永不 FullYOLO）無測——唯一測試把 `_process_heartbeat_loop` 整個 AsyncMock、從不斷言 ctx.auth_level；改 :646 為 FullYOLO 全綠＝提權 | 🏁 ADR-0176 |
| E38 | 0076 | 🟡 MED | ④ | `run_sandbox_command` :247 storage↔memory 位置引數對調無測——wiring 測試只驗 `callable`、值測試繞過呼叫點直呼 helper | ⬜ |
| E40 | 0078 | 🟡 MED | ④ | 同端點 `pulse_count` 夾制 1..5 無測（無 Pydantic backstop）——loop 被 mock、測試只送預設 1、改 :651 拿掉夾制全綠 | ⬜ |
| E41 | 0079 | 🟡 MED | ⑧ | `mcp_hub.sync()` curated 同名優先（survive-sync）無測——兩測名稱互斥、反轉 :145 迭代序（外部覆寫本地）全綠 | ⬜ |

### E39 · heartbeat retrigger 強制 SafeAuto 無測（ADR-0078，🔴 HIGH，家族③，verify CONFIRMED）——🏁 已釘死（ADR-0176）
- **不變式**：UI 一鍵重新觸發須把背景脈衝鎖 SafeAuto、永不升級 FullYOLO（HITL 審批仍生效）。`heartbeat.py:646` 設值後傳入 `_process_heartbeat_loop`（:658-663）。
- **假綠**：`test_heartbeat_retrigger.py` 唯一派發測試用 `AsyncMock` 整個替身掉背景迴圈、只斷言 202/清單、**從不 assert ctx.auth_level**；另兩測停 403/404 前置分支不到 :646。scripts/ 無守衛掃 retrigger/SafeAuto。變異 :646→FullYOLO 全綠＝擁有者按鈕讓背景迴圈以完全自主權執行工具、繞過審批＝提權。
- **⚠️ 注意**：生產碼現況正確（是 SafeAuto），此為**覆蓋缺口非 live 缺陷**（對比 E35 是 live）。
- **治本**：mock 改 side_effect 捕獲 ctx（add_task 第 4 位置引數）斷言 `auth_level=="SafeAuto"`。**可與 auth-level 家族守衛（0021/G16/0154）綁一族。**

### E38 · run_sandbox_command 位置引數對調無測（ADR-0076，🟡 MED，家族④，verify CONFIRMED）
- `evolution_security.py:247` `_build_rlimit_preexec(memory_mb, storage_mb, cpu_sec)`；wiring 測試只斷言 `callable(preexec_fn)`、值測試以 keyword 直呼 helper 繞過 :247。變異對調前兩引數→全綠、RLIMIT_FSIZE 儲存上限被設成 256MB 記憶體值（~5× 弱化）。釘死＝wiring 測試 invoke 捕獲的 preexec_fn 斷言 FSIZE==storage*MB、AS==memory*MB。

### E40 · heartbeat retrigger pulse_count 夾制無測（ADR-0078，🟡 MED，家族④，verify CONFIRMED）
- `heartbeat.py:651` `max(1, min(req.pulse_count, 5))`，無 Pydantic Field 約束。同 E39 loop 被 mock、測試只送預設 1。變異拿掉夾制→全綠。呼叫者須過 is_owner+can_awaken（非跨身分）、下游 pulse_slots 部分 backstop＝MED。釘死＝送 999/負數斷言派發上限 5/1。**與 E39 同端點同測試檔可一次補。**

### E41 · mcp_hub sync() curated 同名優先無測（ADR-0079，🟡 MED，家族⑧，verify CONFIRMED）
- `mcp_hub_service.py:145` `for s in (*curated, *official, *smithery)` + name.lower() seen-set 去重＝curated 迭代序在前勝出（survive-sync 核心）。兩測名稱互斥（mcp-docgen vs Git）或走 no_update 提前返回、無同名衝突。變異反轉迭代序（外部覆寫本地）→全綠＝外部同名清單靜默覆寫 curated 條目連同 env 哨兵/vault_jail_arg。釘死＝curated 與 official 共用同名、斷言存活者為 curated。

### 塊8 降級為 LOW（假綠成立但嚴重度校準，4 條）
- **0073 Decision#3 真實 Agent ID 匿名化無守衛 → LOW**：docs/tests 零機器執法（一次性人工清查）；dev-time/揭露層、需主動改壞、tests/README.md:11 文件慣例 backstop。**治本建議＝比照 ADR-0171 建 guard_id_anonymization.py 掃 docs/+tests/ 黑名單。**
- **0076 write_mcp_implementation venv/pip 無資源上限 → LOW**：ADR-0076 不變式明文只 scope 到 verify 步；venv 固定命令、pip 碼執行與 rlimits 正交、POSIX-prod-only+Docker cgroup backstop。
- **0078 read_json corrupt 告警 → LOW**：兩例外分支回傳值皆 None（行為恆等）、裝飾性日誌、真正 fail-fast 由 validate_live_configs 獨立承接。
- **0079 install() 未過 is_command_safe → LOW**：下游 StdioServerParameters 走 exec 非 shell＝metachar 惰性、/hub/install Owner gated、curated 種子另有驗；需手動改壞 mcp_hub.json 才可達（家族⑥⑧）。

### 塊8 LOW 面值（~5）
- L-0073 smoke_group_dispatcher 型別（②，pyright 未接 CI+smoke 不收集）／L-0074a MAX_CATALOG_TOTAL_BYTES/FILES 累加無直接測試（③）／L-0074b 開源授權準入零強制（doc-only，curate 只警告不擋）／L-0075a set-state-in-effect 根治無正向回歸釘死（PLAUSIBLE）／L-0075b SessionItem 簽章型別鎖 vitest 不驗（③，靠 tsc 另條管線）。

### 塊8 值得記的 REAL
- **ADR-0074**：catalog 安裝安全 8 條全 REAL（已審 id/40-hex commit 拒 branch 且斷言未抓取/路徑穿越/逐檔 sha256 完整性/8 身分矩陣/20MiB 上限）。
- **ADR-0078**：韌性群多條 REAL（原子寫入 tempfile+fsync+os.replace/tool_executor 錯誤類別/degraded 三家真 payload/interrupted runs 落盤/terminate_process_tree killpg+taskkill/T/upsert 併發鎖）。
- **ADR-0077**：Skill Hub 退役經 guard_dead_code(vulture/knip)+grep 全驗。

---

## 塊9 追加（ADR 0080–0086，2026-07-22）

> 續掃第三塊。**5 MED 確認（E42–E46，無 HIGH）＋~5 LOW＋2 降級為 LOW**；0086 無痕（zero-residue）2 條隱私缺口為本塊重點，但其四道核心後端閘皆 REAL（核心零殘留成立、缺口是邊角）。ADR-0082（dispatch 併入 proxy）全乾淨。原始資料 scratchpad `early_c9.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E42 | 0080 | 🟡 MED | ⑤③ | artifact 協調 wiring——唯一測試把 `useArtifactExtractor` 整支 mock、零斷言呼叫時機；砍 `if(activeStream)` 閘或刪 finalize 分支全綠 | 🏁 ADR-0184 |
| E43 | 0081 | 🟡 MED | ③ | `STREAM_ACTIVE_MAX_SECONDS` 孤兒 active 串流收割無測——_sweep elif 分支從不觸及（測試只走 TTL if 分支）；改 `elif False` 全綠→孤兒協程/記憶體洩漏 | 🏁 ADR-0183 |
| E44 | 0085 | 🟡 MED | ③ | ask_user headless 廣告閘（tool.py:106 非互動不注入）無測——反轉/刪閘全綠→心跳/SNS 自主 agent 被注入 ask_user 卡住等人 | 🏁 ADR-0183 |
| E45 | 0086 | 🟡 MED | ③隱私 | incognito 摘要子閘門 inject_context_summary 雙重遮蔽（測試不 mock 它+ctx 無 session_id 使其 no-op）；移出 if 區塊全綠→真人續聊帶舊 session_id 洩漏先前摘要 | 🏁 ADR-0179 |
| E46 | 0086 | 🟡 MED | ③④隱私 | 前端全域匯出過濾無痕（useSessionEvents.ts:96）無測——唯一匯出測試 mockSessions 無 incognito、G17 守衛不含此檔；移除過濾全綠→匯出備份 JSON 殘留無痕對話 | 🏁 ADR-0179 |

### E42 · artifact 協調 wiring 零斷言（ADR-0080，🟡 MED，⑤③，CONFIRMED）——🏁 已釘死（ADR-0184）
- ChatProvider.tsx:139-154（即時 artifact 只對 active、active 完成 finalizeStream 回填 staticHistory）。唯一測試把 useArtifactExtractor 整支 mock（processStreamChunk/finalizeStream），那兩名字只出現在 mock 行 never in expect()。變異砍 :144 `if(activeStream)` 或刪 :147 finalize 分支全綠。釘死＝斷言呼叫時機/條件。

### E43 · 孤兒串流收割無測（ADR-0081，🟡 MED，③，CONFIRMED）
- stream_buffer_service.py:201-209 _sweep elif（active 超齡 cancel producer+標 error）。grep STREAM_ACTIVE_MAX_SECONDS 測試零命中；test_ttl_sweep 先 finish→走 if 分支永不到 elif。變異 :201 `elif False:` 全綠→handler 卡死時孤兒 producer 無限佔記憶體+task。唯一回收者無 backstop＝MED。釘死＝推時鐘過上限+不 finish 觸發 _sweep 斷言 error/cancelled。

### E44 · ask_user headless 廣告閘無測（ADR-0085，🟡 MED，③，CONFIRMED）
- tool.py:106 `if ask_user and not resumable_stream: continue`（雙層防線外層/廣告閘）。三處回歸防線皆不斷言 ask_user 在/不在 names——test_scorecard...injects_memory_tools 以 falsy resumable_stream 真跑 ToolLayer 卻只查 memory_tools。變異反轉/刪閘全綠→自主 agent 被注入 ask_user 卡住。內層哨兵僅在未傳 _emit_event 時觸發＝與外層獨立、自主有 _emit_event 時失效。釘死＝A/B 對釘 ask_user not in / in names。

### E45 · incognito 摘要子閘門雙重遮蔽（ADR-0086，🟡 MED，③隱私，CONFIRMED）
- soul.py:57-60 inject_home_messages 與 inject_context_summary 同在 `if not incognito` 區塊。測試只 mock/斷言 inject_home_messages、對 inject_context_summary 零斷言，且 ctx 無 session_id→變異移出 if 後內部 `if not session_id: return`（soul_service.py:242）靜默 no-op 遮蔽。真實可達：monitor.py:79 由 request.session_id 寫 ctx，無痕請求可攜 session_id→先前對話摘要注入 AI 讀到→違反失憶。釘死＝對 inject_context_summary 亦 AsyncMock+assert_not_awaited、ctx 設非空 session_id。

### E46 · incognito 全域匯出過濾無測（ADR-0086，🟡 MED，③④隱私，CONFIRMED）
- useSessionEvents.ts:96 `sessions.filter(s => !s.incognito)`。唯一 handleExport 測試 M2 的 mockSessions 無 incognito→排除分支從不驅動、只驗 auth_level+base64；G17 源碼守衛區塊不含此檔（incognito.test.tsx 註解「匯出過濾由守衛把關」＝願景未落地）。變異 :96 `sessions.map(...)` 全綠→匯出備份 JSON 殘留無痕對話內容。釘死＝mockSessions 含 incognito+正常各一斷言結果不含無痕 id。

### 塊9 降級為 LOW（2）
- **0083 對比守衛正規式盲區 → LOW**：a11y_basics 正規式只比對斜線透明度 `text-muted-foreground/70`、對等效兩 class `opacity-70`/裸 opacity 全盲，**且已是 live 生產碼**（SoulStudio:390 疊 opacity-70＝A5 親點名／DebugMonitoringView/ConnectionItem/ChatArea/ToolApprovalCard 裸 opacity）。降級因＝純顯示層 WCAG 對比、斜線形態仍執法。**釘死＝守衛加測兩 class 共存+裸 opacity 疊文字。**
- **0084 feedback.db 隔離邊界 → LOW**：測試只斷言路徑字串不含 /soul//legacy/、無「無其他 IO」行為斷言；submit() 加寫 FactualStore/legacy 全綠。降級因＝生產乾淨、需主動改壞、soul 一路有 test_io_prison backstop。

### 塊9 LOW 面值（~5）
- L-0080 SessionItem Loader2 spinner（顯示）／L-0082a .dockerignore 語意（③ 只驗規則行不驗真排除）／L-0082b DispatchSupervisor 刻意不註冊 process_registry（「不做某事」無測，加 register 全綠→可被 /stop 誤殺外部 AI）／L-0084 檢視頁不外露 agent_id（③ 缺 negative-assertion）／L-0085 SSE ask_user_request wiring（③ 源碼字串）／L-0086 SessionItem 無痕徽章（顯示）。

### 塊9 值得記的 REAL
- **ADR-0082 全乾淨**：dispatch bind host secure-by-default（127.0.0.1+源碼掃描）、DISPATCH_AUTOSPAWN 預設 False（雙層驗零 spawn）、supervisor env 逐欄斷言、看門狗封頂退避重生+stop 後不重生。
- **ADR-0086 四道核心後端閘 REAL**：Session 落盤跳過/記憶工具不注入/家對話不注入/payload log 不寫（真 HTTP 端到端旗標貫穿）＋前端 syncSession 收口＝核心零殘留成立。
- **ADR-0084**：GET /feedback/recent Owner-only（真 403）+跨身分過濾；**ADR-0081**：resume/active/stop IDOR 守衛+StreamBufferService 狀態機 11 案例；**ADR-0085**：ask_user_store 暫停-續跑+逾時哨兵。

---

## 塊10 追加（ADR 0087–0093，2026-07-22）

> 續掃第四塊。**4 新 MED 確認（E47/E48/E49/E51）＋1 重複（E50=E45）＋1 OVERTURNED（第 5 個推翻）＋~4 LOW**；0088/0089/0090 全乾淨（**0089 唯讀工具 auto-allow 守衛扎實**）。原始資料 scratchpad `early_c10.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E47 | 0087 | 🟡 MED | ③ | `request.deep_research→ctx` wiring（proxy.py:64/102）無 router 層測試——直呼 SkillLayer 測試手設 ctx 免疫；改 `=request.incognito` 或刪行全綠 | 🏁 ADR-0183 |
| E48 | 0091 | 🟡 MED | 韌性 | 搜尋供應者「服務異常」fallback（tavily/searxng/ddg except Exception→None）無測——只測缺金鑰不測拋例外；縮小 except 範圍全綠（ADR 頭號主張未測） | ⬜ |
| E49 | 0092 | 🟡 MED | ⑦ | 滑動視窗 per-session Lock 併發序列化無測——只驗 `_get_lock` 物件身分不驗真並行；移鎖全綠（heartbeat+chat 雙寫競態） | 🏁 ADR-0182 |
| E51 | 0093 | 🟡 MED | ④ | i18n {{var}}/<Trans> parity「自動驗證」未落地——Guard #33 只比 key 名稱集合不比 value 內插結構；已存在未偵測不對稱（en 124 vs zh-TW 116 個 `{{`） | ⬜ |
| E50 | 0092 | (重複) | ③⑧ | incognito 不注入 context_summary＝**同 E45**（0086/0092 兩 ADR 表面化同缺口，強化修復信號，不另計） | ⬜ 併 E45 |

### E47 · deep_research wiring 無 router 層測試（ADR-0087，🟡 MED，③，CONFIRMED）
- proxy.py:64/:102 `ctx["deep_research"]=request.deep_research`。唯一觸及 skill.py:74 消費點的 test_deep_research.py 手設 ctx 直呼 SkillLayer、不經 proxy.py；router 測試 mock process_proxy_request 只斷言 status/header 不送 deep_research/不查 ctx。變異改 `=request.incognito` 或刪行全綠。釘死＝router 測試 POST {deep_research:true} 捕 ctx 斷言 True。

### E48 · 搜尋供應者「服務異常」fallback 無測（ADR-0091，🟡 MED，韌性，CONFIRMED）
- research.py:117/155/200 except Exception→None（供應者執行期例外視同不可用續試下一個＝ADR 頭號韌性主張）。test_research_providers.py 只 monkeypatch config 空字串（缺金鑰）從不讓 get()/json() 拋例外；唯一注入真實 transport 例外的是 google 分支（走它獨立 try/except）。變異 :155 `except Exception:`→`except ValueError:`（漏 httpx 例外）15 測試全綠。釘死＝讓 searxng/ddg get() 拋 ConnectError 斷言續試下一供應者。

### E49 · 滑動視窗 Lock 併發序列化無測（ADR-0092，🟡 MED，⑦，CONFIRMED）——🏁 已釘死（ADR-0182）
- sliding_window_service.py:306-307 `async with self._get_lock(session_id):` 序列化 heartbeat+chat 雙觸發點（禁令 57）。唯一鎖測試只斷言 _get_lock 物件身分（同 session 同鎖）從不呼叫 check_and_trim；真呼叫者全單次循序、兩觸發點測試把 check_and_trim AsyncMock。變異移鎖直接 return inner→全綠。釘死＝asyncio.gather 併發兩 check_and_trim 同 session 斷言序列化。

### E51 · i18n parity 自動驗證未落地（ADR-0093，🟡 MED，④，CONFIRMED）
- Guard #33（guard_i18n.ts）只比對 flatten key 名稱集合（extra/missing/empty），**從不擷取 value 內 {{...}}/<n> markup 跨語言比對**；無獨立 i18n parity 腳本；i18n.test.ts 只手斷言 ~5 key。**已存在未偵測不對稱：en 124 vs zh-TW 116 個 `{{`**。變異把 en 某含 {{model}} value 改純文字（key 仍在）→全綠、UI 內插靜默消失。釘死＝逐 key 擷取 zh-TW/en 的 {{name}} token 集合斷言相等。

### E50 = E45（重複，不另計總帳）
- ADR-0092 亦獨立發現 incognito 不注入 context_summary（soul.py:56-60）——與塊9 E45（ADR-0086）**同 file:line/同測試/同變異**。0086 擁 incognito、0092 擁 context_summary，交集缺口自兩 ADR 表面化＝**強化此缺口確需修的信號**。

### OVERTURNED（第 5 個推翻假陽性）
- **0091 Google 金鑰走 x-goog-api-key header（禁令 23）其實有真執法**：analyze 誤以 test_research_google.py 為唯一涵蓋，**verify 找到 `test_gemini_key_header_security.py`**（註冊 research.py 為 GEMINI_CALLER、讀真實源碼、banned regex `params={"key"` + 斷言 `x-goog-api-key` 字面）；提議變異會翻紅兩現通過測試＝非假綠。典型漏看 SSOT 安全測試。

### 塊10 LOW 面值（~4）
- L-0091a follow_redirects=False（禁令 22；analyze 標 LOW 但 verify overturn 提及有 AST 檢查 scoped _search_google＝可能已守，待確認）／L-0091b SearXNG/DDG timeout=5s（顯示/文件延遲）／L-0093a nonExplicitSupportedLngs 刻意不開（稱已測實無回歸測試）／L-0093b errors.json 橋接完整性（只抽樣 ~5 碼，有 fallback 到 defaultValue backstop）。

### 塊10 值得記的 REAL
- **ADR-0088/0089/0090 全乾淨**：Guard #32 permissions↔registry、**唯讀工具 auto-allow（安全相關守衛扎實）**、Tavily 調校。
- **ADR-0091**：auto 鏈序（短路斷言 httpx 不呼叫）、tool_retry emit、DDG 畸形隔離、全鏈失敗依狀態碼分流診斷、Google 金鑰 header（SSOT 安全測試）皆 REAL。
- **ADR-0092**：pct_invariant validator、min_keep_pairs 保底、換模型冷卻、is_home 觸發、context_summary 注入替換式皆 REAL。
- **ADR-0093**：Guard #33 key-sync（真跑 npm 對真實檔）、fallbackLng、i18nContext 退役（knip）、LOCALE_ENDONYMS 皆 REAL。

---

## 塊11 追加（ADR 0094–0100，2026-07-23）

> 續掃第五塊（多為守衛/config 本體）。**3 MED 確認（E52/E53/E54，皆家族③「wiring 無測」）＋~3 LOW＋2 降級為 LOW**；0095/0097 守衛乾淨、**0094 成本煞車核心 REAL**。原始資料 scratchpad `early_c11.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E52 | 0096 | 🟡 MED | ③ | `validate_live_configs()` 啟動接線無測——所有自動化測試設 TOUKA_BOOTSTRAP_ENABLED=false→main.py:186 永不執行；刪行/靜默吞錯全綠（=ADR 要修的舊行為復辟） | 🏁 ADR-0181 |
| E53 | 0099 | 🟡 MED | 跨服務 | Guard #36 provider parity run_checks 失敗分支無測——pass 分支+測試複製 set-diff 自我斷言；`if missing:`→`if missing and False:` 全綠 | ⬜ |
| E54 | 0100 | 🟡 MED | ③ | Claude adapter 工具轉換出場接線無測——所有 fixture 硬編 request.tools=None→:104-110 truthy 分支從不執行；方向反轉/刪快取斷點全綠（=「收不到工具」故障復辟） | 🏁 ADR-0177 |

### E52 · validate_live_configs 啟動接線無測（ADR-0096，🟡 MED，③，CONFIRMED）——🏁 已釘死（ADR-0181）
- main.py:186 lifespan 內 `if TOUKA_BOOTSTRAP_ENABLED` 後呼叫 validate_live_configs()（壞 live 設定 fail-fast 中止啟動）。**所有自動化測試（conftest.py:35/147+smoke sandbox_env.py:45）在 src 匯入前設 flag='false'**→lifespan 整段跳過、line 186 永不執行；6 單元測試直呼函式本體不 import main。變異刪 :186 或 `try:...except:pass`（=ADR-0096 要修的舊靜默吞錯復辟）→全綠、壞設定被 read_json 吞回預設（=成本 cap/quota 失效根因）。**ADR 本身要解決的核心風險類別重演。** 釘死＝flag='true' 注入壞 live 檔 `pytest.raises(ConfigValidationError)` with TestClient。

### E53 · provider parity 失敗分支無測（ADR-0099，🟡 MED，跨服務契約，CONFIRMED）
- guard_provider_parity.py:46-58 run_checks（backend registry 金鑰 env 須在前端 providerKeys.ts）。test_parity_passes_currently 只走 pass 分支（當前一致）；test_drift_is_detected_by_set_diff **繞過 run_checks 在測試內重算 set-diff 自我斷言**（家族③）。變異 L53 `if missing:`→`if missing and False:` 五測全綠。後端加供應商漏前端金鑰輸入項→守衛靜默通過（唯一防線）。釘死＝monkeypatch discoverable_specs 回傳額外 spec 呼叫 run_checks 斷言非空。

### E54 · Claude adapter 工具轉換接線無測（ADR-0100，🟡 MED，③，CONFIRMED）——🏁 已釘死（ADR-0177）
- claude_adapter.py:104-110（出場 openai_to_anthropic_tools+apply_tool_cache_breakpoint）。**test_adapter_contract.py _claude_request() 硬編 request.tools=None、全部 prepare_request 測試無一覆寫**；golden 亦不設 tools→truthy 分支從不以真實資料執行。tool_schema.py 單元測試只驗函式**直接呼叫**正確不驗 adapter 串接。變異 :106 方向反轉（=「收不到工具」故障復辟）或刪 :108-110 快取斷點→全綠。釘死＝設 request.tools=非 None 斷言 kwargs["tools"] 為 Anthropic 形狀+末項 cache_control ephemeral。

### 塊11 降級為 LOW（2）
- **0094 GET/POST /autonomy quota/cap HTTP 往返 → LOW**：test_autonomy_sync 不含/不斷言 cap/quota；唯一 round-trip 純 Pydantic 層。**降級因**＝載重不變式（cap/quota 一級欄位不丟）由 test_budget_cap 強力 backstop、**POST 落盤用 model_dump() 獨立 response_model**（router 變異只藏讀回視圖不致資料流失、成本煞車仍存）、需主動改壞。家族⑧。
- **0098 ESLint ratchet 比較邏輯無單元測試 → LOW**：三註冊測試只查檔案+字串；變異 `*1000`/sys.exit(0) 全綠。**降級因**＝守衛透過 guard_all 在 CI 對真實 ESLint 活體執行（真 any 仍被攔）＝現行 backstop、dev-tooling 面。

### 塊11 LOW 面值（~3）
- L-0097 tsconfig noUnusedLocals 旗標值無測（改 false/刪無人守，只有 knip/ESLint 部分 backstop）／L-0098b 缺 baseline 檔 fail-closed（dev-only 邊角）／L-0099b run_checks 缺檔分支。

### 塊11 值得記的 REAL
- **ADR-0094 成本煞車核心 REAL**（「修復永遠失效的成本煞車」）：quota/cap 讀真實 config、model round-trip（修 extra=ignore）、is_over_budget >=邊界值釘死、**真驅動 claude/openai/gemini 三家 handler 停自主迴圈**（tool await==0+tool_choice=none，行為級，三家共用 react_core.py:217 單一實作點）。
- **ADR-0095/0097 守衛乾淨**；ADR-0096 壞 JSON/schema/extra 鍵/缺檔皆 REAL；ADR-0099 **SSRF 守衛編碼繞過（十進位/十六/八進位/dword IP）全參數化+端到端**、resolve_provider/get_key/apiKey 遮罩皆 REAL；ADR-0100 tool_schema SSOT 轉換器 20+ 條+Gemini 組合子剝除皆 REAL。

---

## 塊12 追加（ADR 0101–0107，2026-07-23）

> 續掃第六塊。**2 MED 確認（E55/E56）＋~7 LOW＋多條降級**；0101/0105 乾淨。**⭐三支安全相關 ADR 核心皆 REAL 無 confirmed MED**（0104 破壞性重置／0106 頭像安全部分／0107 密碼閘）。原始資料 scratchpad `early_c12.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E55 | 0102 | 🟡 MED | ③ | skill↔MCP 啟動接線（main.py:214-231 migrate+validate）無測——測試只直呼純函式；刪整段全綠＝回退「無聲失敗」（ADR 自證「機制由單元測試守、CI no-op」） | 🏁 ADR-0181 |
| E56 | 0106 | 🟡 MED | ⑧ | avatars 納入備份（SANCTUARY_FOLDERS）無測——姊妹 legacy/vault 有斷言 avatars 獨缺；刪 "avatars" 字串全綠→還原時頭像全失（ADR B2 明載的測試缺席） | ⬜ |

### E55 · skill↔MCP 啟動接線無測（ADR-0102，🟡 MED，③，CONFIRMED）——🏁 已釘死（ADR-0181）
- main.py:214-231 lifespan 兩段（migrate_stale_server_keys→真時 registry.load_config 重載；validate_skill_mcp_wiring→逐筆 logger.warning）。test_skill_mcp_wiring.py 全 6 測直呼純函式傳 tmp_path、零 lifespan 引用；Guard #37 純靜態不碰 main.py。變異刪整段→全綠、舊 key 使用者靜默連不上 MCP（=docgen bug 復辟）。釘死＝integration 測 boot lifespan 後斷言 load_config 被呼叫+caplog '⚠️ [Skill-MCP]'。

### E56 · avatars 備份清單成員無測（ADR-0106，🟡 MED，⑧，CONFIRMED）
- manage_backup.py:19 SANCTUARY_FOLDERS 含 "avatars"+collect_items() rglob 收錄。唯二 collect_items 測試只建/斷言 legacy/vault/根層 db、從不建 data/avatars/*；test_system_backup 全 mock subprocess。變異刪 "avatars"→全綠、頭像靜默漏出備份不可逆遺失。釘死＝test_collect_items_includes_avatars（建檔斷言 arcs+清單成員）。

### 塊12 降級為 LOW（4，含 1 推翻性降級）
- **0102 Guard #37 derive_server_key 鏡像 → LOW**：守衛本地硬複製未 import src SSOT、test 只驗複製品；變異 src 規則守衛假陰性（docgen bug class）。降級因＝需主動改 SSOT+validate_skill_mcp_wiring 啟動 WARNING backstop。**釘死＝跨檔一致性測試（guard_key(n)==src_key(n) 對 "a_b" 等）。**
- **0103 smoke import ordering（HIGH→LOW 降兩級）**：4 smoke 須先 enable_smoke_sandbox 才 import src（Windows 死鎖）；family② 零自動化。降級因＝測試組態隔離非安全邊界、Windows-only、120s timeout backstop fail-loud。
- **0104 單 store purge 隔離（⭐推翻性降級）**：analyze「無測試讓 purge 拋例外」**事實錯誤**——test_memory_unavailable_forces_not_clean 真使 purge_memory 拋+無 raises 包裹→移 try/except 測試會 ERROR＝隔離有真執法。僅殘留 no-leak 子屬性（res.error 只斷言 truthy、改 str(exc) 全綠）＝LOW。
- **0107 自助清資料 → LOW**：密碼閘/範圍/擁有權核心 REAL。

### 塊12 LOW 面值（~7）
- L-0103b runner 120s timeout（孤兒 runner）／L-0104a 向量 purge 回真實計數（改 return 1 全綠）／L-0104b RETAINED_STORES 排他性／L-0105 群（layout Provider 配置/sonner theme/CTA 白字硬編/sync-design 漂移，display 層）／L-0106 刪 user 連帶刪頭像呼叫點（PARTIAL）。

### 塊12 值得記的 REAL
- **⭐ADR-0104 破壞性重置核心全 REAL**：雙重確認+角色收斂（排除 Debugger、confirm_agent_id 從不流入刪除目標）／assert_resettable 兩道防線（Jin/Yan/Code/大小寫/前綴/空白全拒+列舉失敗保守拒）／備份優先失敗中止／每 store purge+完整性自檢（Jin 資料仍在）／R11 禁 false-clean／R7 TOCTOU 鎖。
- **ADR-0106 安全部分全 REAL**：magic-byte+大小上限（拒 SVG 端到端 422）／結構性無 IDOR（JWT 無 path 參數）／路徑穿越 404／寫入失敗回滾。
- **ADR-0101 計分卡保真全乾淨**（build_base_context SSOT/ToolLayer 真注入/不設 resumable_stream）；**ADR-0105 主題全乾淨**（對比度雙模式 48 斷言真算 WCAG）。

---

## 塊13 追加（ADR 0108–0114，2026-07-23）

> 續掃第七塊（**最高產：9 MED 確認 E57–E65**）＋~6 LOW；0114 乾淨。**⚠️ E65 是第 2 個 LIVE 生產缺口**（繼 E35）：per-agent reset 靜默刪除使用者上傳檔。安全關鍵 ADR 核心仍多 REAL（0110 GDPR/0113 reset 結構/0114 sandbox）。原始資料 scratchpad `early_c13.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E65 | 0113 | 🟡 MED〔HIGH 降〕**LIVE** | — | **per-agent reset rmtree 整個 outputs/ 但聊天上傳刻意巢狀 outputs/uploads/→每次 reset 靜默刪 owner 說要保留的上傳檔**；ADR 前提事實錯誤、cited 測試反而斷言 outputs 全刪 | 🏁 ADR-0175 |
| E57 | 0108 | 🟡 MED | ④ | AES-256 強度未斷言——測試只驗「能加密開啟」；`nbits=256→128` 全綠（跨服務契約 i18n/SECURITY/0110/0112 複用） | ⬜ |
| E58 | 0108 | 🟡 MED | ⑧③ | download 端點路徑穿越/.zip 防護未觸發——姊妹 delete 有 non-zip 測試 download 無；移防線全綠 | 🏁 ADR-0179 |
| E59 | 0109 | 🟡 MED | ⑦ | backup_execution_lock 併發互斥無測——測試零引用鎖名/無 asyncio.gather；移 `async with` 全綠（排程+手動備份重疊損壞） | 🏁 ADR-0182 |
| E60 | 0110 | 🟡 MED | ④⑥ | GDPR vault 根圍堵（root_escapes_app_data）未觸發——測試恆使 root 在 app_data 內或回 None；刪整塊全綠（agent_id 穿越 over-read） | ⬜ |
| E61 | 0111 | 🟡 MED | ③ | ClaudeAdapter.prepare_request 溫度夾 1.0+top_p/k 轉發無測——fixture 硬編 None（**同 E54 _claude_request 盲點**）；改硬寫 1.0/刪轉發全綠＝譯期靜默丟棄迴歸 | 🏁 ADR-0177 |
| E62 | 0111 | 🟡 MED | ③⑥ | 群聊 apply_native_defaults 呼叫端無測——函式本體有測、group.py:151 wiring 無斷言；刪行全綠（群聊與單聊分歧） | 🏁 ADR-0183 |
| E63 | 0112 | 🟡 MED | ③⑤ | settings-snapshot 熱載入 _RELOADERS 無測——fixture `monkeypatch _RELOADERS={}` 清空；_run_reloaders 改 pass 全綠（admin 匯入後 stale in-memory 直到重啟） | 🏁 ADR-0181 |
| E64 | 0112 | 🟡 MED | ③ | settings-import 成功路徑無測——只測 wrong 密碼/dry_run（反而斷言 create_backup 未呼叫）；去備份/去鎖全綠（喪失還原點+TOCTOU） | 🏁 ADR-0181 |

### E65 · per-agent reset 刪使用者上傳檔（ADR-0113，🟡 MED〔verify HIGH→MED〕，⚠️ LIVE）——🏁 已修（ADR-0175）
- **owner 決策（D1）保留使用者上傳聊天檔**，ADR 前提「既有原語只清 outputs/missions 子目錄天然不碰上傳檔」。**但 get_uploads_path（resource_manager.py:116-129）刻意把上傳巢狀在 `<vault>/outputs/uploads`**（docstring 明載），purge_outputs（primitives.py:89-95）rmtree 整個 outputs/ 樹∈ALL_STORES→**每次 per-agent reset（及共用原語的 DebugAgent path）靜默刪上傳檔＝ADR 前提事實錯誤**。假綠＝test_custom_agent_identity_survives_reset 從不建 outputs/uploads/*、line 191 反斷言 outputs 全刪。降級因 agent-scoped+orchestrator backup-first backstop。**治本＝purge_outputs 排除 outputs/uploads/ 子樹。**
- 其餘 E57-E64 詳見 scratchpad `early_c13.md`（每條含 file:line/變異/釘死）。**共通：E61/E62/E63/E64 皆「函式被測、接線/成功路徑無測」家族③；E59 家族⑦併發鎖只物件耦合；E57 家族④只驗能加密不驗強度；E58/E60 安全圍堵防線觸發分支從不走到。**

### 塊13 LOW 面值（~6）
- L-0108 Semaphore(2) 節流（⑦）/加密暫存成功路徑清理／L-0109 GET /backup/schedule non-admin（⑧）／L-0110 總量 1GiB 上限（多檔累計）/併發匯出 Semaphore（⑦）／L-0111 pipeline 精準度層序（各層獨立測未貫穿真 pipeline）／L-0113 assert_agent_resettable ownership leg（唯一 admin-only 呼叫端 require_admin 使 is_owner bypass 恆 True＝零實效，ADR 自陳保留供未來）。

### 塊13 值得記的 REAL
- **ADR-0110 GDPR 匯出核心幾乎全 REAL**：JWT-only 無 IDOR/密碼再驗/passphrase≥12/白名單脫敏/**對話專案範圍化含 G-2 owner_user_id 修復**/facts SQL agent_id 範圍/逐檔 symlink 逃逸/單檔上限/明文暫存即刪。僅 vault 根圍堵是 E60。
- **ADR-0113 reset 結構防線 REAL**：兩道結構地板（agent_id SQL+is_protected_path）/guard 核心決策/端點縱深序（admin→CSRF→密碼→雙 confirm→guard→backup-first）/orchestrator backup-first+false-clean+共用鎖。僅 uploads-under-outputs 是 E65 live。
- **ADR-0112 REAL**：匯出遮罩/匯入回填不覆寫真金鑰/驗證管線 sha256/合併語意/端點閘。僅熱載入+成功路徑缺口。
- **ADR-0114 全乾淨**：transpile 絕不執行使用者碼（禁 eval/new Function 靜態掃+真惡意碼斷言 globalThis 未污染）；ADR-0108 加密 round-trip、0109 排程分支、0111 NativeModelLayer/is_native_owner SSOT 皆 REAL。

---

## 塊14 追加（ADR 0115–0121，2026-07-23）

> 續掃第八塊。**1 HIGH（E66）＋4 新 MED（E67-E70）＋1 重複（E36 自 0118 再現）＋1 OVERTURNED（第 6 個）＋3 降級**；0116 乾淨。安全關鍵 ADR 核心仍多 REAL（0119 檔案動詞/0120 跨身分隔離）。原始資料 scratchpad `early_c14.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E66 | 0115 | 🔴 HIGH | ③ | **vault-jailed MCP 咽喉注入（tool_executor.py:540-542 唯一 apply()）無測——測試只手造假物件直呼 apply()、真跑 executor 皆 protocol_manager=None；刪 3 行全綠＝fails open cross-agent vault 讀（#1 鐵則）** | 🏁 ADR-0176 |
| E67 | 0115 | 🟡 MED | ⑥ | D4 install vault_jail_arg 傳遞無測——grep test 零命中；刪 if 全綠（重裝丟 vault jail 政策） | ⬜ |
| E68 | 0117 | 🟡 MED | 供應鏈 | torch CPU-only 鎖定無守衛——無測掃 uv.lock 是否混 nvidia/CUDA；換 index 來源不改 spec 字串、Guard#30 恆綠（映像暴增 GB） | ⬜ |
| E69 | 0119 | 🟡 MED | ③ | identity/project_id args-merge 序無測——server 鍵在 `**args` 後遮蔽模型偽造鍵（身分主權）；無測餵 args 內 identity 鍵、調 merge 序全綠 | 🏁 ADR-0180 |
| E70 | 0120 | 🟡 MED | ③ | _execute_core 逾時→terminate_process_tree 接線無測——所有測試 mock 掉 _execute_core；刪殺樹呼叫全綠（逾時後孤兒孫進程） | 🏁 ADR-0182 |

### E66 · vault-jailed MCP 咽喉注入無測（ADR-0115，🔴 HIGH，③，CONFIRMED）——🏁 已釘死（ADR-0176）
- ADR-0115 #1 鐵則+D1：檔案 MCP 存取永不跨 agent、vault 根由驗證身分決定模型不可指定。tool_executor.py:540-542 apply()＝唯一咽喉注入（覆寫 args[vault_jail_arg] per-agent，fail-closed）。**test_mcp_vault_jail.py 全 8 測手造假 _Reg/AgentIdentity 直呼 J.apply()、從不經 executor；真跑 execute_to_queue 皆 protocol_manager=None（MCP 分支不進）**。變異刪 3 行→全綠、**fails OPEN cross-agent vault 讀（sandbox/IDOR 隔離破壞）**；D3 env backstop 只限 app_data 根非 per-agent 不能替代。**⚠️ 生產現況正確（注入在位）＝覆蓋缺口非 live；HIGH 因安全鐵則+變異 fails open**。釘死＝mock protocol_manager resolve_tool 回 server_id，餵模型 base_dir='/etc' 斷言 call_tool_on_server 收到 agent vault 根非 /etc。
- E67/E68/E69/E70 詳見 scratchpad `early_c14.md`（各含 file:line/變異/釘死）。**共通：E66/E67/E70 皆「函式/本體被測、接線無測」家族③（被 mock 遮蔽）；E69 身分主權 merge 序 defense-in-depth（下游 vault_tools 從 identity.agent_id 重錨＝偽 identity AttributeError＝MED 非 HIGH）；E68 供應鏈換 index 來源不改 spec 字串。**

### 重複＋推翻
- **E36 自 0118 再現**：apply_switch「絕不 re-embed／動 FactualStore」無負向斷言＝與 ADR-0070 同產碼（embedding_service.py:162-174），verify 明標「非新缺口」（第 3 次跨 ADR 表面化）。
- **OVERTURNED（第 6 個）0119 CRITICAL_TOOLS 空參安全早退其實有真執法**：analyze 稱只在 smoke（不收集），verify 找到 `test_critical_tool_empty_args_returns_validation_error`（真呼叫 execute_to_queue("update_my_soul",{}) 斷言 validation_error）+守衛 name-agnostic+test_critical_tools_covers_new_names 把新動詞釘進 frozenset＝組合完整；提議變異會翻紅。（0120 亦分析同不變式、DOWNGRADED-LOW，TypeError backstop）。

### 塊14 降級為 LOW（3）＋LOW 面值
- **0117 Heartbeat --package 精簡子集（MED→LOW）**：guard_dependencies 寫死 --package 沙盒不讀 Dockerfile、import awaken 無 torch-absent 斷言；降級因需主動改 Dockerfile+映像膨脹無安全+Guard#30 lock-staleness 部分 backstop。**0120 CRITICAL_TOOLS 三命令空參（→LOW，TypeError backstop）**。**0121 演化管線 4→2（降級 LOW，核心 REAL）**。
- LOW 面值：0115 D3 env sentinel 接線／0117 chown -R 遞迴（映像倍增）／0118 hf-cache volume chown YAML 漂移／0120 wait_seconds 60 上限（測試皆 ≤3）+is_structured_payload _STATUS_PREFIX（拿掉 status 前綴 approval_pending 被注時間尾綴破壞 json.loads）。

### 塊14 值得記的 REAL
- **ADR-0116 全乾淨**（ToukaMemory 整合收斂）；**0117** Guard #30 Check1 逐項變異+Guard #16 沙盒真 import+proxy --all-packages（僅 E68/heartbeat）；**0118** resolve_env_file_path SSOT/apply_runtime_env factory 即時金鑰/前端 apply 失敗不前進/容器 is_dir 分歧 422/下載 additive/切換 confirmation（僅 E36）；**0119** Tier1 policy fail-safe DENY/幻覺偵測含動態 MCP/Gemini 剝組合子/**Tier3 檔案動詞家族（soul write-scope 拒/edit-undo/create_only/edit_soul 只委派寫/Guest 9 動詞）**/CRITICAL_TOOLS 空參早退（真測）（僅 E69）；**0120** 跨 agent 結果隔離（execution_id 綁 agent_id 跨身分 resource_not_found）/running signals/terminate_process_tree 真子進程（僅 E70；**核心後果已被 0143 HITL 反轉、聚焦仍活不變式**）；**0121** 廣告翻轉/狀態機驗序核心 REAL。

---

## 塊15 追加（ADR 0122–0128，工具遙測/觀測性系列，2026-07-23）

> 續掃第九塊。**2 MED（E71/E72）＋1 OVERTURNED（第 7 個）＋2 降級**；0122/0126 乾淨。**🔑 E72 是 claude_adapter `_claude_request` fixture tools=None 盲點第 3 現（E54 工具/E61 取樣/E72 快取斷點同源）。⭐G-3 危險 background 徽章經掃描確認 REAL。** 原始資料 scratchpad `early_c15.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E71 | 0125 | 🟡 MED | ③ | `_execute_core` 真子進程逾時(600s/30s)+逐行增量寫入無測——所有測試 mock 掉 _execute_core、訊號測試餵合成 record；改 timeout=1/刪增量寫入全綠 | 🏁 ADR-0182 |
| E72 | 0127 | 🟡 MED | ③ | apply_tool_cache_breakpoint 套用點死碼——`_claude_request` 硬編 tools=None 使 claude_adapter.py:110 從不執行；刪呼叫全綠→每回合重複計費 ~6346 tok | 🏁 ADR-0177 |

### E71 · _execute_core 真子進程逾時+增量寫入無測（ADR-0125，🟡 MED，③，CONFIRMED）
- terminal.py:141/327-338 硬逾時 backstop（背景600s/前景30s 兩層看門狗第一層）+:322-323 逐行 record output/last_output_at（唯一驅動 stalled/runaway 訊號的活資料源）。所有 execute_command/_run_background 測試 patch _execute_core（AsyncMock/_blocking_core 忽略 timeout）、訊號測試餵合成 _running_record()、test_process_group_kill 只單測 terminate_process_tree 不經 _execute_core。變異 timeout 600→1/刪增量寫入/砍 TimeoutError 分支全綠。釘死＝integration 跑真 sleeper+monkeypatch 極小 timeout 斷言增量輸出+「timed out after」經真殺樹。

### E72 · apply_tool_cache_breakpoint 套用點死碼（ADR-0127，🟡 MED，③，CONFIRMED）——🏁 已釘死（ADR-0177）
- claude_adapter.py:106-110 於 tools 轉 Anthropic 後掛快取斷點（P4 核心）。**`_claude_request()` 硬編 request.tools=None、全檔無 Claude 測試設非空 tools→`if _anthropic_tools:` 恆 False→套用點從不執行**（誘餌 test_claude_tool_result_cache_breakpoint_rotates 測 messages 斷點非 tools）。變異刪 apply_tool_cache_breakpoint→全綠、每 ReAct 回合重複計費 ~6346 tok（Owner 27 工具）＝整支 P4 靜默吞無 backstop。釘死＝設非空 tools 斷言 kwargs['tools'][-1]['cache_control']=ephemeral。**⚠️ 與 E54/E61 同 fixture 盲點，單一修可關三。**

### OVERTURNED（第 7 個）
- **0124 舊子字串啟發後備其實有真執法**：analyze 稱無測試以純字串工具走完整 execute_to_queue，verify 找到 test_tool_events_emit.py 的 `_ok_tool`（回純字串 "done"）經 execute_to_queue 抵 legacy 分支斷言 status==success；提議變異（is_success 只剩 is_tool_success）實測翻紅。analyze 漏 tests/proxy/test_tool_events_emit.py。

### 塊15 降級為 LOW（2）
- **0123 H4 Manual variant interactive/headless wiring（→LOW）**：pure function 有測 fail-closed 措辭、但 ToolLayer 把 resumable_stream 轉 interactive（tool.py:238）在 headless 情境未驅動（測試全寫死 resumable_stream=True）。
- **0128 system 前綴快取套用點（→LOW，推翻性降級）**：functional half（system 組裝含降級警告）其實有 2 integration 測試（analyze「整段從未執行」事實錯誤）；caching half（block 轉換+anthropic-beta header）真未涵蓋但破壞只靜默 cache miss=成本。

### 塊15 LOW 面值
- L-0122 duration_ms perf_counter 非 wall-clock（NTP 倒退無測）/sse_line SSOT 無守衛擋硬編（ADR 遞延）；L-0125 stop_command 對 pending _task 的 task.cancel()（record 預設 _task=None 從未執行）。

### 塊15 值得記的 REAL
- **ADR-0122 全乾淨**（ToolCallEvent SSOT/wire 凍結+additive/Guard #38 AST 擋字面量繞封套活體/extract_error_class 容忍時間尾綴）；**ADR-0126 全乾淨**（parseToolResult 依 error_class 分流）。
- **ADR-0125**：running signals（stalled≥20s/runaway≥256KB 確切門檻）/訊號僅諮詢絕不自動 kill/stop_command 跨身分隔離 404/**⭐G-3 危險 background 預核准保留⚠️徽章不繞審批 REAL＝G-3 扎實**（僅 E71）。
- **ADR-0124**：build_tool_success/is_success 結構化權威（❌不誤判）/edit-insert _applied_hunk（legacy 後備亦有測=OVERTURNED）；**0127** 純函式契約 4 測+Anthropic-only 隔離（僅 E72）；**0128** to_cached_system 純函式+遙測 round-trip+system functional integration（僅 caching-half LOW）；**0123** validation_protocol tag/Manual vs Autonomous/tool-presence gate 皆 REAL。

---

## 塊16 追加（ADR 0129–0135，2026-07-23）

> 續掃第十塊。**2 MED（E73/E74）＋3 降級**；**⭐0131 認證續期/LDH DebugAgent 挾持收斂、0132 session owner_user_id 授權地基＝兩支安全地基 ADR 全乾淨、0135 亦乾淨**。原始資料 scratchpad `early_c16.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E73 | 0130 | 🟡 MED | ③ | claude_adapter reorder→split 生產接線無測——`_claude_request` 無 system 使 :114-120 死碼；刪 reorder 全綠（**第 4 現 fixture 盲點**，僅成本迴歸） | 🏁 ADR-0177 |
| E74 | 0134 | 🟡 MED | ⑤ | subagent 子 loop 600s 硬上限無測——fake stream 立即完成、唯一直呼 _drain_text 繞過 wait_for；刪逾時全綠（背景 Task 洩漏） | 🏁 ADR-0182 |

### E73 · claude_adapter reorder→split 接線無測（ADR-0130，🟡 MED，③，CONFIRMED）——🏁 已釘死（ADR-0177）
- claude_adapter.py:111-120 須 reorder→split 順序+同組 VOLATILE_SYSTEM_TAGS。`_claude_request()` 無 system→:114-120 恆不執行；唯一到達的 test_model_prompt_ssot 只 `assert agent_id in str(system_param)`（字串化無法區分 reorder 是否執行）。變異刪 reorder 全綠。失效爆炸半徑僅快取命中率=成本。**⚠️ 與 E72/E54/E61 同 claude_adapter.py:106-120 fixture 盲點群：工具轉換/取樣/快取斷點/reorder→split 皆因 _claude_request 不設 tools 也不設 system 死碼。單一 fixture 修（帶真實 assembled system+tools 的 Claude 測試變體）可一次關 E54/E61/E72/E73 4 MED + 0128/0129 2 LOW。**

### E74 · subagent 子 loop 600s 硬上限無測（ADR-0134，🟡 MED，⑤，CONFIRMED）
- subagent_tools.py:54 `_SUB_TIMEOUT_SECONDS=600.0`+:206 `asyncio.wait_for(_drain_text(stream), ...)`＝唯一防背景子任務無限跑 backstop（ADR 邊界②承認背景子 loop 不隨父中斷取消）。所有測試 fake stream 立即完成/monkeypatch _run_subagent；唯一直呼 _drain_text 者繞過 wait_for 包裹。變異刪逾時/改 60000.0 全綠＝背景 Task 洩漏至進程重啟、get_stream_generator 無等價硬上限。釘死＝永不 [DONE] 的 fake stream+monkeypatch 極小 _SUB_TIMEOUT 斷言 TimeoutError+deliver FAILED。

### 塊16 降級為 LOW（3）＋LOW 面值
- **0129 穩定前綴細分同一 claude_adapter 接線（→LOW）**：同 E73 生產點，0129 verify 降 LOW（reorder behavior-neutral 冪等/no-op；破壞只降快取命中=成本）。**註：0129/0130 同一接線點 verifier 校準略異（LOW/MED），實為同一 claude_adapter wiring 缺口群。**
- **0133 Context Snapshot 重寫（→LOW）**（真 pipeline dry-run 核心 REAL）。
- **0134 決策 H1 auth_level 組裝/執行一致性（MED→LOW）**：subagent_tools.py:116 組裝 coalesce `or "Manual"` vs :188 執行不 coalesce→父 auth_level=None 時不一致無測；**降級因＝唯一歧異 None→兩者皆落 safety.py 決策表嚴格 CONFIRM 分支＝fail-safe 方向非提權+default-CONFIRM backstop**。
- **L-0130 層界守衛名不副實（家族④）**：ADR 稱 Guard #21 把關 adapters 不 import services，但 .importlinter 唯一 contract 只禁 memory_server→src.*、**無禁 src.adapters→src.services**；tool_schema.py 加 `from src.services...` guard 仍全綠＝守衛檢查的是別的方向。

### 塊16 值得記的 REAL
- **⭐ADR-0131 全乾淨**：認證續期/LDH 收斂（根除 DebugAgent 身分挾持）——經前作守衛線認證縫批（ADR-0163 等）+本掃描雙證扎實。
- **⭐ADR-0132 全乾淨**：Session owner_user_id 授權地基（G-2 之基礎）。**ADR-0135 乾淨**（結構化回報+finalize；stop_gate 已 0146 退役）。
- **ADR-0134**：遞迴根絕雙閘（Guest 無 dispatch_subagent 真 PermissionService+組裝剝除工具 schema 真 _assemble）/子代理臨時身分不落盤+衍生 request_id+無 session_id REAL（僅 E74/H1）；**0129/0130** 純函式全 REAL（VOLATILE_SYSTEM_TAGS SSOT/切分/reorder 逐字保真/遙測 round-trip）；**0133** 真 pipeline dry-run 核心 REAL。

---

## 塊17 末塊追加（ADR 0136–0139，2026-07-23，收官塊）

> **3 MED（E75/E76/E77）＋2 降級**。**⚠️ E76 是第 3 個 LIVE 生產缺口；E77 是 near-HIGH 安全覆蓋缺口。ADR-0137 對抗式互審子系統多數載重不變式 REAL（雙層 gate/自簽防線/TOCTOU/fail-closed/隔離皆扎實）。** 原始資料 scratchpad `early_c17.md`。

| ID | ADR | 嚴重 | 家族 | 一句話 | 狀態 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| E76 | 0137 | 🟡 MED **LIVE** | ③ | **互審成本閘 cost_ceiling_exceeded 生產恆 False**——mission_tools 對 run_review_round 從不傳 config=→config 恆 None→limit 恆 None→成本閘永不觸發、ADR「不可關」承諾失效 | 🏁 ADR-0175 |
| E75 | 0137 | 🟡 MED | ⑥③ | has_passing_assertions（反橡皮圖章第 3 件）未接入 hard_checks_pass——只用於顯示字串；改 return True 只該函式單元測試變紅 | 🏁 ADR-0183（生產修） |
| E77 | 0138 | 🟡 MED（near-HIGH） | ③安全 | dispatch token 路徑穿越 sanitize 無測——callback_token 模型可控、_token_path 唯一防護、下游無 backstop；移 sanitize 全綠＝arbitrary-file-read 迴歸 | 🏁 ADR-0178 |

### E76 · 互審成本閘生產恆 False（ADR-0137，🟡 MED，③，⚠️ LIVE）——🏁 已修（ADR-0175）
- review_loop.py:156 `cost_ceiling_exceeded(parent_identity, config=config)`，但**唯一生產呼叫端 mission_tools.py:216-227 對 run_review_round 未給 `config=`**→config 恆預設 None→`(None or {}).get('cap') or {}`=={}→limit=None→**函式對所有生產流量恆 False**。ADR 決策 4「成本閘無視 cap.enabled 不可關」在生產**永遠不觸發**不論 owner 設多少 max_total_cost_usd。假綠＝測試直呼 pure function 手組 config／loop 測試 config=None 分支恆死／wiring 測試 monkeypatch run_review_round。**治本＝mission_tools 補 `config=`（從 autonomy config 取）。**

### E75 · has_passing_assertions 未接入 hard_checks_pass（ADR-0137，🟡 MED，⑥③）——🏁 已修（ADR-0183 生產修）
- 反橡皮圖章第 3 件套（test_log 斷言數 >0）應如另兩件硬性影響 hard_checks_pass，但 has_passing_assertions（evidence.py:45）唯一呼叫點是 bundle_digest 顯示字串；真 gate hard_checks_pass 只算 cmd_results.ok+artifacts_present（test_reviewer.py:207 釘死 empty→True 真空真）。無 verification_cmd+一任意 artifact 恆過、producer 可偽造 test_log。變異 return True 只 4 行單元測試變紅。有 LLM critic AND backstop 故 MED。**治本＝hard_checks_pass 在 test_log 非空時亦要求 has_passing_assertions 為真。**

### E77 · dispatch token 路徑穿越 sanitize 無測（ADR-0138，🟡 MED〔near-HIGH〕，③安全）——🏁 已釘死（ADR-0178）
- callback_token 是 mission_tools(await_dispatch_result) 模型可控輸入；`_token_path`（subagent_results.py:41 `_TOKEN_SAFE_RE.sub`）是唯一防護、下游 resource_manager 無 resolve()/is_relative_to（is_protected_path 只護 legacy/soul）。全 8 測只用安全 token 從不餵 '../'。變異 `safe=token[:128]`（移 sanitize）全綠→callback_token='../../../other_vault/secret' 逃出 dispatch_results/＝arbitrary .json 讀取（read 端渲染回模型＝跨 vault/秘密外洩）+persist 覆寫。**生產現況正確（sanitize 在位）＝覆蓋缺口非 live，但迴歸即靜默出貨 arbitrary-file-read＝near-HIGH。治本＝加 test_token_traversal_rejected 斷言 resolve 落在 dispatch_results/ 內。**

### 塊17 降級為 LOW（2）＋REAL 亮點
- 降級：0136 LIVE episode 組裝依身分（eval-harness dev-only+2 行 forwarder+生產身分轉發另有覆蓋）／0139 harness_context（區塊組裝+Guard #40 核心 REAL）。
- **⭐ADR-0137 多數載重不變式 REAL**：雙層 DONE 落盤 gate（含 save_mission 後門 HIGH REAL）/record_review 自簽防線+can_review（HIGH REAL）/TOCTOU stale 化/parse_critique 全 fail-closed（HIGH REAL）/EvidenceCollector no-op+pytest 零收集硬拒/review_loop 隔離高風險同模型 BLOCKED 不呼 critic（HIGH REAL）/ReviewBudgetLedger 真並發防超帳/臨時身分不落盤。
- **ADR-0138 多數 REAL**：並發上限真並發 peak 斷言/崩潰隔離/子任務數上限/遞迴閘 Guest/並行身分互異不落盤/耐久雙寫真 vault round-trip/await 耐久優先跨重啟不空等。**ADR-0136** 非 LLM oracle 四類機械裁決真檔 AST/gate 指標零分母 None。

---

## 🏁🏁 全線收官（0001–0139 除退役 0072，2026-07-23）

- **總帳**：**76 條 HIGH/MED 確認（6 HIGH／70 MED）＋~105 LOW 面值＋7 推翻假陽性**。E01-E34（0001-0064，塊1-6）+E35-E77（0065-0139，塊7-17，除 E50=E45 重複）。
- **6 HIGH**：E01/E02/E03/E04（0021/0022/0044/0047）**已修**（批②④①三高槓桿 ADR-0169~0171＋批 A ADR-0172）；**E39/E66 🏁 批②＝ADR-0176（07-23）行為級測試釘死＝6 HIGH 全數收官**（皆覆蓋缺口、生產現況正確；今後變異 fails open/提權即紅）。
- **🏁 3 條 LIVE 生產缺口（皆 MED、生產真的壞）全數修復＝ADR-0175（2026-07-23，批 LIVE）**：**E35**（delete_skill is_system 讀頂層鍵→改 read_touka_field，巢狀＋round-trip 雙釘死）、**E65**（reset rmtree outputs/ 吞上傳→purge/remaining 一致排除 uploads＋RETAINED 列示）、**E76**（互審成本閘 config 未傳→mission_tools 補 config=autonomy_service.config＋端到端 BLOCKED 測試）。皆紅→綠變異驗證。
- **🔑 單一 fixture 修可關 4 MED+2 LOW**：E54/E61/E72/E73＝claude_adapter.py:106-120 全 tools/system wiring 因 `_claude_request()` 不設 tools 也不設 system 而死碼。**🏁 批③＝ADR-0177（07-23）帶 tools/取樣/assembled-system 變體 4 測全關（OpenAI ingress 測 E54/E61；native ingress 測 E72/E73——轉換器預埋斷點會遮蔽 E72 變異故必須 native）；5 變異全紅、0128/0129 LOW 同點一併觸及。**
- **near-HIGH 安全覆蓋缺口**：E77（dispatch token 路徑穿越 sanitize 無測，生產正確但迴歸即 arbitrary-file-read）——**🏁 批④＝ADR-0178（07-23）7 測釘死（結構監獄邊界×5＋read 外洩誘餌＋persist 覆寫對稱）；M-A 移 sanitize 7 測全紅、M-B 窄化 backslash token 紅**。
- **7 推翻**：0010/0011/0023/0024＋0091（gemini key SSOT）＋0119（CRITICAL_TOOLS）＋0124（legacy 後備）＝adversarial opus verify 主動找到 analyze 漏看的真執法測試（sonnet analyze grep 太窄的假陽性由 opus verify 攔下）。
- **最系統性假綠型態＝家族③「wiring/接線無測」**：SSOT 函式/守衛邏輯/純函式孤立驗證扎實，但生產呼叫端/接線/成功路徑/失敗分支無回歸（E47/E52/E54/E55/E61-E64/E71-E73/E75/E76…）＝本 repo 最普遍。次為⑦併發只物件耦合無真並行（E28/E43/E49/E59/E74）、⑧不對稱姊妹（E33/E34/E56/E58）、④只驗存在不驗值/等級/強度（E05-E07/E51/E57）、②smoke 不收集（E02/0103）、⑥被測函式生產無呼叫端（E41/E75）。
- **⭐ 好消息：安全關鍵 ADR 核心普遍 REAL**：0104 破壞性重置/0106 頭像/0107 密碼閘/0110 GDPR/0113 reset 結構/0114 sandbox/0131 LDH DebugAgent 挾持/0132 owner_user_id 授權地基/0137 互審自簽+雙層 gate 皆多 REAL＝缺口集中邊角/接線而非安全核心。
- **owner 拍板修碼優先序建議**：①**3 LIVE 缺口修碼 🏁 ADR-0175（07-23）**（E35 read_touka_field／E65 purge_outputs 排除 uploads／E76 mission_tools 補 config=）②**2 未修 HIGH 補行為測試 🏁 ADR-0176（07-23）**（E39 heartbeat retrigger auth_level／E66 vault-jail 咽喉注入）③**單一 fixture 修關 claude_adapter 群 🏁 ADR-0177（07-23）**（E54/E61/E72/E73）④**near-HIGH E77 補路徑穿越回歸測試 🏁 ADR-0178（07-23）**⑤家族③/⑦系統性補接線測試（分 ⑤-A～⑤-F 六子批，比照三高槓桿）：**⑤-A 安全邊界 🏁 ADR-0179（07-23）**（E21/E22/E45/E46/E58）＋**⑤-B 身分主權/RBAC 🏁 ADR-0180（07-23）**（E24 guest fallback／E27 dispatch 非特權／E69 executor call_kwargs／E19 terminal auth_level／E31/E32 dispatcher 可見性）＋**⑤-C 啟動/設定收斂 🏁 ADR-0181（07-23）**（E52 bootstrap fail-fast／E55 skill↔MCP 啟動接線／E63 匯入熱載入／E64 成功路徑備份+鎖／E26 mission 200 序列化；lifespan TestClient 骨架落地）＋**⑤-D 真併發/真行程 🏁 ADR-0182（07-24）**（E28 跨行程 file lock／0049×5 filelock 家族含 FileLockAsyncRule check_Call 守衛強化／E49 per-session 鎖 gather 序列化／E59 備份執行鎖／E70 逾時殺樹／E71 真子進程增量+逾時／E74 子 loop 硬上限；gather+in-flight 探針與真 shell 子進程夾具兩骨架落地）＋**⑤-E 派工/串流雜項 🏁 ADR-0183（07-24）**（E20 橋接 DI／E25 扁平遷移／E29 終態回呼 trigger／E43 孤兒收割／E44 ask_user 閘 A/B／E47 deep_research 貫穿／E62 群聊取樣一致／**E75 生產修＝hard_checks_pass 接 has_passing_assertions＝backlog 生產缺口全數歸零**）＋**⑤-F 前端 vitest 🏁 ADR-0184（07-24）**（E30 x-touka-skills header 建構／E42 artifact 協調呼叫時機）。**🏁🏁 修碼線全線收官（07-24）＝76 條 HIGH/MED 全數處置：14 批（ADR-0169～0172／0175～0184）、4 生產修（E35/E65/E76/E75）、2 守衛強化（Guard #44 誕生／FileLockAsyncRule check_Call）**；E23 🏁 ADR-0185（CI fresh-volume 所有權寫入探針＋chown 涵蓋面靜態釘死）＝**76+1 全數處置**；~105 LOW 面值留檔備查。
- **產物**：本 backlog（E01-E77 living doc）＋scratchpad early_c1_FINAL/early_c2..c17.md（逐條 file:line/變異/釘死）＋memory `touka-guard-adr-review`。**均未 commit（owner 慣例自行）。**
- **方法**：`Workflow scriptPath=wf_early_scan_chunk.js args={adrs:[...7 篇...],pending:[]}`；縮塊 7 篇/run（~900K tok）、high/med 都覆核、LOW 面值；每塊跑完落 scratchpad + 更新本表。
- **收官後**：合併全 0001–0139 缺口，比照前作分批（可一起建的守衛綁批、優先序=爆炸半徑），各守衛建成另立 ADR 回填狀態。**家族①（ruff 納閘）、家族②（smoke 升格）、家族④（Guard #22/#24 等級辨識）為三個「一次治多條」的高槓桿治本點。**
