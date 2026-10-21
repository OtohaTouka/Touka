# ADR-0252：規則減負：根 AGENTS.md 去重複、禁令把關名稱更正、ADR 歸檔存根

- 狀態：✅ 已接受（Owner 2026-10-15 核准規則減負計畫；D1～D4 由第一個 PR 落地，D5 由第二個 PR 落地，已歸檔清單見本檔「已歸檔清單」）
- 日期：2026-10-15
- 決策者：Owner
- 影響範圍：`AGENTS.md`（核心守則 15 句、禁令 15／41／47／49、刪「核心能力清單」）、`README.md`「核心功能」、`tests/README.md` Guard #28 說明、`HANDOVER.md`、`docs/CHANGELOG.md`；第二個 PR：`docs/architecture/adr/` 已歸檔的 ADR 換成存根、部分被推翻的加現況行
- 相關：ADR-0068（ADR 編號主權與 Guard #28）、ADR-0173（Guard #28 第 4 項）、ADR-0251（Windows 前置條件段，本 ADR 不動）。這些 ADR 的決策都不變。

## 背景

- 根 `AGENTS.md` 77,874 B（LF）、385 行、65 條禁令。同一件事在核心守則與禁令各寫一次，改一處容易漏另一處；部分禁令寫的把關名稱已經過時。
- 有人提議把每條禁令縮成一行、全文另放，估計禁令段可從約 28 KB 降到 6～8 KB。事先定下「不丟任何要求」的原則後，把幾種做法都實際寫出來量過（D1）。
- 已退役或只記流水帳的 ADR 仍是全文，搜尋時雜訊很多；其中有些檔內狀態還寫「已接受」，直接打開會被誤導。

## 決策

### D1 三種減法的實測與結論

| 做法 | 實測（基準 `8b2e242e`，UTF-8、LF） | 結論 |
|---|---|---|
| 每條禁令縮成一行、全文另放 | 65 條扣掉 2 條墓碑與 7 條沒有完整機器把關、到處適用的禁令，剩 56 條：27,135 B → 18,333 B；加上「何時讀全文」的對照表與說明，整份只省 5,913 B，而且兩處要同步維護 | 不做 |
| 條文瘦身：只搬純理由、事故經過與沿革 | 15 條、22 段、1,820 B；每條加回背景指向後淨省 1,400 B（1.8%） | 低於事先定的 3 KB 門檻，不做 |
| 去重複：核心守則被禁令原文完整涵蓋的句子改成「見禁令 N」或刪 | 逐句判斷 52 句：15 句可改，1,651 B；其餘 37 句有禁令沒寫到的內容 | 做（D2） |

- 「禁令段降到 6～8 KB」的估計來自只寫主題的索引行，會丟掉要求；根檔大部分內容是真的要求。
- 根檔另有非規則內容：「核心能力清單」1,897 B 是產品介紹（D3）。
- 結論：在不丟要求的前提下，常駐規則檔能減的約 5%；真正的減量在 ADR 歸檔（D5）。

### D2 去重複的判定原則

- 一句話的每個要求與事實都在某條禁令原文裡（完整涵蓋）才改成「見禁令 N」或刪；多出任何內容（定義、具體名稱、範圍、例外、機制）就整句保留。純理由不算要求。
- 以判定當時的禁令原文為準；判定後不改那些禁令原文。
- 刪句後只調連接詞讓清單項讀得通，不改要求。
- 15 句：§2 自動領卡握手 2 句（禁令 16）；§3 唯一 SSOT、前端對等（27）；§7 物理路徑禁令（41）；§8 整合模式單一部署第 3 句（6、14）；§10 只有一行「(下略)」，刪掉那行、標題保留不留缺號；§11 路徑執法的概括句（41）；§13.5 FAILED 狀態特權重置（31）、產出物欄位解耦（32）、C1 第 2 句（34）、H1（35）、H2（36）、Mission Token 委派隔離第 2、4 句（40）。
- 例：Mission Token 那條的「外部 session 的 method allowlist 嚴禁包含 `dispatch_initialize_mission`」保留：禁令 40 只規定呼叫會被拒絕，沒寫 allowlist 這個機制。這條刪兩句後在句末加「其餘見禁令 40」，所以 15 句實際省 1,631 B（上表的 1,651 B 沒算這 21 B，也沒算 §10 那行的換行）。

### D3 核心能力清單移到 README

- `AGENTS.md` 末段「核心能力清單」（11 項）刪掉。`README.md`「核心功能」沒有的 10 項照原文加在該段末（拿掉 ⭐ 標記）；「向量記憶」README 已有「長期語義記憶」，不重複。
- 清單裡唯一像規則的「CREATE 不覆寫既有 id」在禁令 52 原文。

### D4 禁令條文的更正與補充

- 禁令 41：把關名稱改成現況：`lint_ssot.py` 的 `PATH_LITERAL_SSOT`，加上 ruff TID251（`ruff.toml` 的路徑 API 禁用，Guard #44）。
- 禁令 47：已不存在的 `requirements.txt` 換成 Guard #16 現在檢查的對象：各 workspace 成員在單一 `uv.lock` 下的依賴（沙盒匯入）。比對 `pyproject.toml` 與 `uv.lock` 是 Guard #30（禁令 53）的事，不寫進這條。
- 禁令 49：設定裡沒有的 ESLint 規則 `tailwindcss/no-arbitrary-value` 改成 Guard #4（`ToukaChat/scripts/guard_design.ts`）。
- 禁令 15：補一句「歷來公開過的值列在 `config.PUBLIC_SECRET_VALUES`」，只是指向。公開值在哪些情況仍會被接受，見 ADR-0251 已知限制。
- 禁令 42 的特權白名單比 `lint_ssot.py`、`ruff.toml` 的實際白名單窄，這次不改（已知限制）。
- 禁令開頭格式與編號都不變，`tests/unit/test_documentation_contracts.py` 的禁令清單檢查不用改。

### D5 ADR 歸檔用存根

- 已退役、或只記流水帳的 ADR，整份換成約 8 行的存根，保留原檔名與編號：
  - 標題：照原標題。
  - 狀態：「⛔ 已歸檔：由 ADR-Y 取代」（編號放在「由」後面），或寫明歸檔的原因。
  - 一句話說明這份 ADR 的價值現在在哪（哪份 ADR、哪個測試或守衛）。
  - 全文位置：`git show <commit>:docs/architecture/adr/<原檔名>`。
  - 原本的修訂回指，每個編號都帶 `ADR-` 前綴，Guard #28 第 4 項仍找得到。
- 編號、索引列與所有指向它的連結都不變；Guard #28 四項檢查與「ADR 編號主權」不用改。內文仍有效的 ADR 不歸檔。
- 存根與說明文字裡，反轉性動詞（修訂、取代等）後面不直接接 ADR 編號：Guard #28 第 4 項會把這種寫法當成正向修訂宣告，要求被提到的 ADR 回指。
- 已歸檔清單見下方「已歸檔清單」。

## 已歸檔清單

- 下列 42 份換成存根，原檔名與編號不變；全文：`git show 6acb88bd:docs/architecture/adr/<原檔名>`。內文仍有效的 ADR-0160、ADR-0193 不歸檔。
- 部分內容被後來的 ADR 推翻的 19 份在標題下加一行「現況」，其餘原文不改：ADR-0008、ADR-0011、ADR-0012、ADR-0016、ADR-0018、ADR-0021、ADR-0038、ADR-0043、ADR-0056、ADR-0060、ADR-0061、ADR-0073、ADR-0079、ADR-0083、ADR-0123、ADR-0134、ADR-0135、ADR-0140、ADR-0197。

| ADR | 歸檔原因 | 價值現在在哪 |
|---|---|---|
| [ADR-0048](0048-dispatch-handshake-no-token-param.md) | 由 ADR-0050 取代 | `dispatch_handshake` 的 token 參數設計見 ADR-0050。 |
| [ADR-0057](0057-pydantic-copy-deprecation.md) | 變更紀錄型 | 成果在 `tests/unit/test_pydantic_copy_deprecation.py`。 |
| [ADR-0058](0058-vitest-v4-pool-options-migration.md) | 變更紀錄型 | 成果在 `tests/unit/test_vitest_config_schema.py`；`ToukaChat/tsconfig.guard.json` 已含 `vitest.config.ts`（Guard #27）。 |
| [ADR-0063](0063-skill-hub-external-sync.md) | 由 ADR-0077 取代 | Skill Hub 已退役，技能探索由 ADR-0074 的真技能目錄承接。 |
| [ADR-0072](0072-skill-hub-curated-manifest-and-pinned-sync.md) | 由 ADR-0077 取代 | Skill Hub 已退役，技能探索由 ADR-0074 的真技能目錄承接（沿用本 ADR 的釘 commit 做法）。 |
| [ADR-0159](0159-providers-auth-self-heal-parity.md) | 變更紀錄型 | 成果在 `ToukaChat/tests/guard/providers_auth_self_heal.test.ts` 與 `ToukaChat/src/lib/providers/__tests__/` 的三份 `*.authSelfHeal.test.tsx`。 |
| [ADR-0162](0162-guard-coverage-high-gaps.md) | 變更紀錄型 | G03 的成果在 `tests/unit/test_ldh_cardless_no_type_flag.py`；G01／G02 的 Guard #42／#43 已依 ADR-0238 退役。 |
| [ADR-0163](0163-guard-coverage-auth-seam-gaps.md) | 變更紀錄型 | 成果在 `test_soul_stepup_audit.py`、`connections.logout.test.ts`、`AuthContext.cardOrder.test.tsx`、`test_monitor_authlevel_before_rebind.py`、`providers_auth_self_heal.test.ts`。 |
| [ADR-0164](0164-guard-coverage-antidrift-gaps.md) | 變更紀錄型 | 成果在 `test_soul_model_registration_cache.py`、`test_dispatch_subagent.py`、`test_sse_consumers_exclude_reasoning.py`、`test_run_react_loop_pumps_send_round.py`。 |
| [ADR-0165](0165-guard-coverage-frontend-wiring-gaps.md) | 變更紀錄型 | 成果在 `connections.skillApprovalEvent.test.ts`、`StreamRegistry.test.tsx`、`page.screenshot.test.ts`。 |
| [ADR-0166](0166-guard-coverage-idor-family-gaps.md) | 變更紀錄型 | 成果在 Guard #41（`scripts/guard_role_plane.py` 的混合角色字面量規則）、`test_guard_role_plane_mixed_literals.py`、`test_soul_crud_real_writes.py`。 |
| [ADR-0167](0167-guard-coverage-deploy-gaps.md) | 變更紀錄型 | 成果在 `test_heartbeat_config_dir_mount.py`、`test_env_persistence_anchor.py`。 |
| [ADR-0168](0168-guard-coverage-low-gaps-final.md) | 變更紀錄型 | 成果在 `test_heartbeat_gitignore.py`、`test_soul_model_registration_cache.py`、`test_soul_crud_real_writes.py`、`tests/proxy/test_core_keepalive.py`。 |
| [ADR-0169](0169-early-gap-smoke-promotion.md) | 變更紀錄型 | 成果在 `tests/unit/test_io_prison.py`、`tests/integration/test_env_bootstrap.py`。 |
| [ADR-0170](0170-early-gap-endpoint-admin-level-guard.md) | 變更紀錄型 | 成果在 `scripts/guard_endpoint_auth.py`（等級辨識、GRANDFATHER 空清單斷言）、`scripts/endpoint_auth_admin_required.py`、`tests/integration/test_admin_rbac.py`。 |
| [ADR-0171](0171-early-gap-ruff-banned-api-guard.md) | 變更紀錄型 | 成果是 Guard #44：`scripts/guard_ruff_banned_api.py`（docstring 說明用途）與 `tests/unit/test_guard_ruff_banned_api.py`。 |
| [ADR-0172](0172-early-gap-high-severity-behavioral-tests.md) | 變更紀錄型 | 成果在 `test_monitor_authlevel_gate.py`、`test_mission_cross_identity_idor.py`、`test_dispatch_identity_fixation.py`。 |
| [ADR-0174](0174-frontend-npm-cve-remediation.md) | 變更紀錄型 | 前端 CVE 修補的歷史紀錄；Guard #23 的政策部分由 ADR-0222 取代，現行做法見該 ADR。 |
| [ADR-0176](0176-early-gap-high-coverage-behavioral-tests.md) | 變更紀錄型 | 成果在 `tests/integration/test_heartbeat_retrigger.py`、`tests/unit/test_vault_jail_executor_wiring.py`。 |
| [ADR-0177](0177-early-gap-claude-adapter-fixture-blindspot.md) | 變更紀錄型 | 成果在 `tests/unit/test_adapter_contract.py` 的「ADR-0177」節。 |
| [ADR-0178](0178-early-gap-dispatch-token-traversal-regression.md) | 變更紀錄型 | 成果在 `tests/unit/test_subagent_results.py`。 |
| [ADR-0179](0179-early-gap-security-boundary-wiring-tests.md) | 變更紀錄型 | 成果在 `test_c4_c5_c6_c7_security.py`、`test_fetcher_ssot.py`、`test_soul_layer_incognito.py`、`useSessionEvents.test.ts`、`test_system_backup_ops.py`。 |
| [ADR-0180](0180-early-gap-identity-sovereignty-rbac-tests.md) | 變更紀錄型 | 成果在 `test_identity_sovereignty.py`、`test_c4_c5_c6_c7_security.py`、`test_tool_layer_skill_mcp.py`。 |
| [ADR-0181](0181-early-gap-startup-config-wiring-tests.md) | 變更紀錄型 | 成果在 `tests/integration/test_lifespan_bootstrap_wiring.py`、`test_settings_snapshot.py`、`test_artifact_security.py`。 |
| [ADR-0182](0182-early-gap-real-concurrency-process-tests.md) | 變更紀錄型 | 成果在 `test_mission_filelock_crossproc.py`、`test_mission_filelock.py`、`test_lint_ssot.py`、`test_sliding_window_service.py`、`test_backup_scheduler.py`、`test_terminal_real_process.py`、`test_dispatch_subagent.py`。 |
| [ADR-0183](0183-early-gap-dispatch-stream-wiring-tests.md) | 變更紀錄型 | E75 的 `has_passing_assertions` 接線在 `ToukaProxy/src/services/review/reviewer.py`，其餘在 `test_reviewer.py`、`test_dispatch_callback_trigger.py` 等測試；E62 的 `tests/proxy/test_group_native_defaults.py` 已不存在。 |
| [ADR-0184](0184-early-gap-frontend-vitest-wiring-tests.md) | 變更紀錄型 | 成果在 `connections.skills-header.test.ts`、`connections.reasoning.test.ts`、`ChatProvider.artifactWiring.test.tsx`、`ChatProvider.authLevel.test.tsx`。 |
| [ADR-0185](0185-ci-fresh-volume-ownership-probe.md) | 變更紀錄型 | 成果在 `tests/unit/test_docker_compose_config.py`；CI 探針所在的 `release-images.yml` 已依 ADR-0238 刪除。 |
| [ADR-0210](0210-immutable-npm-and-production-image-binding.md) | 由 ADR-0238 取代 | Node／npm 改取自官方 node 映像（ADR-0238 決策 3）；仍在的相關測試是 `tests/unit/test_p8b_supply_chain_gate.py` 與 `tests/unit/test_guard_all_frontend_runtime.py`。 |
| [ADR-0211](0211-mcp-rollout-evidence-contract-ledger-and-oracle.md) | 由 ADR-0238 取代 | 產品內的 MCP rollout evidence 已依 ADR-0238 決策 4 刪除，沒有現行對應。 |
| [ADR-0212](0212-mcp-runtime-rollout-producers-and-artifacts.md) | 由 ADR-0238 取代 | 產品內的 MCP rollout evidence 已依 ADR-0238 決策 4 刪除，沒有現行對應。 |
| [ADR-0213](0213-toukaide-rollout-producer-and-typescript-binding.md) | 由 ADR-0238 取代 | ToukaIDE 的 rollout 契約綁定已依 ADR-0238 決策 4 刪除，沒有現行對應。 |
| [ADR-0215](0215-mcp-trusted-rollout-verifier-and-release-qualification.md) | 由 ADR-0238 取代 | 發版資格驗證與產品內的 rollout verifier 已依 ADR-0238 決策 1、4 刪除，沒有現行對應。 |
| [ADR-0216](0216-mcp-release-authority-contract-and-provider-boundary.md) | 由 ADR-0238 取代 | release authority 已依 ADR-0238 決策 1、4 刪除，沒有現行對應。 |
| [ADR-0217](0217-public-release-authority-v3-bootstrap.md) | 由 ADR-0238 取代 | `ToukaReleaseAuthority` 與 release workflows 已依 ADR-0238 決策 1 刪除；ToukaIDE VSIX 的測試 `tests/unit/test_toukaide_deterministic_vsix.py` 仍在，Marketplace 發行見 ADR-0218。 |
| [ADR-0225](0225-local-release-profile-and-evidence.md) | 由 ADR-0238 取代 | 映像改由 `.github/workflows/ghcr-publish.yml` 直接建置推送，不再有 qualification 與發行證據（ADR-0238 決策 1、2）。 |
| [ADR-0226](0226-debian-local-profile-vulnerability-applicability.md) | 由 ADR-0238 取代 | Dockerfile 依 ADR-0238 決策 3 改回官方映像與 Debian 安全更新；`ToukaChat/scripts/ensure-critical-native.mjs` 仍由 `ToukaChat/Dockerfile.prod` 呼叫，測試在 `tests/unit/test_chat_critical_native.py`。 |
| [ADR-0227](0227-modern-to-modern-local-upgrade.md) | 由 ADR-0238 取代 | 本機升級腳本 `scripts/local_cold_cutover.py` 已依 ADR-0238 決策 1 刪除，沒有現行對應。 |
| [ADR-0233](0233-local-upgrade-baseline-adoption.md) | 由 ADR-0238 取代 | 本機升級腳本 `scripts/local_cold_cutover.py` 已依 ADR-0238 決策 1 刪除，沒有現行對應。 |
| [ADR-0234](0234-owner-local-release-execution.md) | 由 ADR-0238 取代 | Owner-local 發行腳本與階段紀錄已依 ADR-0238 決策 1、6 刪除，沒有現行對應。 |
| [ADR-0235](0235-owner-deployment-historical-evidence-roots.md) | 由 ADR-0238 取代 | Owner-local 首橋接的讀取程式已依 ADR-0238 決策 1 刪除，沒有現行對應。 |
| [ADR-0236](0236-security-candidate-source-and-installed-identity.md) | 由 ADR-0238 取代 | 自編 Node 與 Debian 套件重編已依 ADR-0238 決策 3 移除；保留的 braces 回補見 ADR-0237。 |

## 規則鬆緊

- D2：不變（只刪被禁令原文完整涵蓋的句子）。
- D3：不變（產品介紹）。
- D4：41、49 不變（只換把關名稱）；47 不變（換掉已不存在的對象，改成 Guard #16 實際檢查的內容）；15 加嚴（輕，補指向）。
- D5：不變（全文留在 git）。
- 19 份部分被推翻的 ADR 加現況行（一頁計畫第 6 項）：加嚴（只加說明，原文不改）。

## 決策落點表

| 決策 | 檔案 | 位置 | 測試／守衛 |
|---|---|---|---|
| D2 去重複 | `AGENTS.md` | §2 自動領卡握手、§3 唯一 SSOT／前端對等、§7 物理路徑禁令、§8 整合模式單一部署、§10、§11 路徑執法、§13.5 | `test_documentation_contracts.py`（禁令清單與範圍不受影響） |
| D3 核心能力清單 | `AGENTS.md`、`README.md` | 刪「🧠 核心能力清單」；「✨ 核心功能」段末加 10 項 | — |
| D4 禁令條文 | `AGENTS.md` | 禁令 15、41、47、49 | `test_documentation_contracts.py` |
| D5 存根 | `docs/architecture/adr/` | 42 份存根、19 份檔首加現況行、`README.md` 42 列狀態欄 | `guard_adr_integrity.py`、`test_guard_adr_integrity.py` |
| 說明更正 | `tests/README.md` | `test_guard_adr_integrity.py` 那列：三項 → 四項檢查 | — |

## 考慮過、不採用的做法

- 禁令縮成一行索引、全文另放；條文瘦身：實測見 D1。
- 子資料夾規則檔：Claude Code 只在讀取該資料夾的檔案時才載入子資料夾的 `AGENTS.md`（Bash、搜尋不觸發），Codex 不依編輯的檔載入，不保證被讀到；`.dockerignore` 的 `AGENTS.md` 也只排除根目錄那份。
- 移走「資訊安全主題摘要」：裡面有兩項根檔別處沒寫的要求（狀態修改端點必加角色守衛、Shell 特殊字元要人確認），不動。
- 刪檔只留索引列：要改 Guard #28 的三項檢查與「ADR 編號主權」，也要改 ADR-0068 的決定，還會產生大量死連結。
- 為了 32 KiB 讀取上限搬動章節順序（見已知限制）。

## 已知限制

- 【推測，依官方文件與原始碼，沒有實跑】Codex 預設只讀規則檔前 32,768 B（`project_doc_max_bytes`）。做完後根 `AGENTS.md` 為 74,628 B（LF），32 KiB 落在第 251 行（§11），禁令從第 42,161 B 開始，仍讀不到。這次不處理。
- 禁令 42 的特權白名單比程式窄（列 5 檔，`lint_ssot.py` 與 `ruff.toml` 的實際白名單較多）。
- 9 條禁令只有本機 `guard_all` 擋、CI 不擋（6、9、12、13、14、45、46、49、50）。
- 禁令 27 的前端有兩份 `countRounds()`：`ToukaChat/src/lib/export-utils.ts` 與 `ToukaChat/src/lib/context_usage.ts` 各自實作。
- 以上與禁令 39 的版本門檻、鎖定段裡的重複，都列為之後的候選。

## 驗證

本機 Windows。本任務沒有打任何付費 API，也沒有啟動 Touka；沒有新增或修改測試、守衛與產品程式。

- 根 `AGENTS.md`：77,874 B／385 行 → 74,628 B／370 行（LF）。去重複 −1,631 B、核心能力清單 −1,899 B（含前面兩個空行）、禁令 15／41／47／49 +284 B。15 句的原文在基準 `8b2e242e` 上逐句比對過位置；禁令條文的差異只在 15、41、47、49。
- `5cb2a277` 的工作資料夾，從 PowerShell 設 PATH（`.venv\Scripts` 放最前面，`python` 是工作資料夾 venv 的 3.12.13），包在 fnm Node 24.21.0 裡：
  - pytest 全量（隨機順序，seed 398305725）：8888 passed／7 skipped／6 failed，6 個是 E1 的 `test_local_compose_bindings`；7 個跳過是 symlink 4、只在 POSIX 1、Docker 部署 1、ToukaIDE Marketplace 打包比對 1，與 ADR-0251 的基準相同。跑完沒有沙盒殘留訊息。
  - guard_all：43／43 PASS（前置檢查 Node 24.21.0／npm 11.19.1）。
  - `scripts/guard_adr_integrity.py`：252 份、0001..0252 連續，四項都通過。`test_documentation_contracts.py`、`test_guard_adr_integrity.py`、`test_guard_source_data_boundary.py`：66 passed。
- 第一次跑 pytest（`4d76f12c`）是在 Git Bash 設 PATH：`D:` 開頭的路徑被冒號切開，子程序的 `python` 落到全域直譯器。改用 PowerShell 在 `5cb2a277` 重跑後，結果完全相同（數字與失敗、跳過清單都一樣）。

## 回復

- 不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）；各項可獨立 revert。
- D2、D3 撤回：把刪掉的句子與清單照 git 歷史加回，README 那 10 項可留可刪。
