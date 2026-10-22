# Touka 系統全球單一事實來源 (SSOT Master Map)

## MCP 伺服器與創建試跑的隔離（C2-3）

決策見 ADR-0275。來源與額外資料夾不放入 `server_config.json`；設定、判定、傳輸各有自己的來源，不由 UI 推導權限。

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 來源值、分類值、可隔離／已接線工具、額外資料夾上限 | `utils/isolation_policy.py`（`MCP_ORIGINS`、`MCP_SELF_ORIGINS`、`MCP_THIRD_PARTY_ORIGINS`、`ISOLATION_WIRED_ACTIONS`） | 來源分類、政策、模型與啟動器橋接 |
| 來源分類、引用路徑、部署程式辨識；啟發式只會更嚴 | `utils/mcp_origin.py`（`classify`、`referenced_paths`、`deployed_skills`） | `mcp_launch`、隔離設定端點、演化凍結 |
| 隔離紀錄位置、原子寫入鎖、讀不到／過期語意與程式身分雜湊 | `services/mcp_isolation_store.py`（`McpIsolationStore`、`bound_of`）；`get_config_dir()` 下的 `mcp_server_isolation.json` | Hub、技能安裝、創建、演化、設定存檔與匯入；bootstrap 只播種 `.example` |
| 設定與手動啟動的 API 模型 | `models/mcp_isolation.py`；`routers/mcp.py` 的 GET／PUT isolation 與 restart | `useMcpIsolation`、`useMcpSettings`、手動啟動確認框 |
| 每台的啟動判定、啟動帳本、等待 660 秒、手動同意 | `services/isolation/mcp_launch.py`（`mcp_launcher`、`mcp_launch_book`、`McpManualStart`） | manager、`GET /mcp/list`、`GET /isolation`、創建試跑 |
| 自建／第三方／scaffold 的路徑、APPDATA 對映、雜湊 state 段名；分類及凍結的 cwd／state home 基準 | `services/isolation/mcp_profiles.py`（`mcp_classification_bases` 共用解析基準） | `mcp_launch`、`evolution_command`；自建可在執行器，第三方／scaffold 只在 Proxy |
| 條目環境與啟動器環境分離 | `sandbox_kernel/spec.py`（`exec_env`、`EXEC_ENV_NAME_PATTERN`、保留名稱與 loader 過濾）；`launcher._exec` 限制後合併；`runners.checked_exec_env` 先濾保留名稱 | `mcp_launch`、`executor_session`、`protocols/mcp/environment.py` 的 `launcher_base` |
| 隔離 stdio、EOF 模式、stderr 上限與關閉順序 | `protocols/mcp/isolated_stdio.py`（`McpIsolatedLaunch`、`isolated_stdio_client`）；`utils/mcp_secrets.py` 共用遮罩 | `client`、manager、演化驗證、創建 SMOKE／EVAL |
| 每台連線鎖、generation、重連排程與關機閘 | `protocols/manager.py`（`_judge`、`_reconnect`、`schedule_relaunch`、`begin_shutdown`） | 啟動、設定變更、隔離 listener、隔離 shared stdio 失聯 |
| 創建一次授權的傳遞與本機層級選擇 | `services/creation/kinds.py`（`CreationLaunchContext`）；`services/isolation/policy.py` | 創建工具／HTTP → 引擎 SMOKE、COMMIT、EVAL；scaffold 看本機 |
| 部署凍結、技能鎖、兩檔快照與協定驗證 | `services/isolation/evolution_command.py`、`tools/builtin/evolution.py`（`_verify_mcp`、`_save_deployment`）；配額／路徑守衛在 `services/evolution_security.py` | 四個演化入口、profile 取得、部署與 MCP 協定驗證 |
| 未隔離卡片的內容完整性 | `services/approval_flow.py`、`services/tool_executor.py` | 後端核准閘與 ToukaChat 審批卡 |
| 設定頁隔離徽章／啟動確認／側欄台數 | `McpIsolationBadge`、`McpManualStartDialog`、`IsolationBanner`；資料經 `useMcpIsolation`／`useIsolationStatus` | ToukaChat；原生 Windows 不計未隔離第三方，等待仍提示 |

隔離設定鎖、同意帳本與技能鎖同樣以單一 Proxy 行程為前提（ADR-0207）；要支援多行程須先換跨行程鎖。

## life1：MCP 連線持有者（2026-10-16，ADR-0273）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| MCP 連線持有者（連線的進入與退出都只在這個 task；關閉期限 `OWNER_CLOSE_SECONDS` 7 秒＋`OWNER_CLOSE_GRACE_SECONDS` 1 秒寬限） | `ToukaProxy/src/protocols/mcp/owner.py` `McpConnectionOwner`（`start`、`aclose`、`abort`、`alive`） | `ProtocolManager.connect_server`、`cleanup`；`LegacyMcpHandle.close` |
| 連線放進 pool 的唯一入口（先換再關） | `ProtocolManager._install_handle`（`HandlePool.swap`） | `connect_server` |
| 失聯通知與重連入口（hook 同步；只為隔離 shared stdio 排重連，direct／遠端不重連） | `ProtocolManager._on_connection_lost` → `_connection_lost_hook(key, error, owner)` | owner task；隔離重連排程 |

## C 線第二批第二段：執行器容器（2026-10-16，ADR-0272）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 控制通道的 frame 格式、上限、ERROR 代碼、ipc 檔名、權杖與 `boot_id` 格式、`block_ports_digest` | `ToukaProxy/src/sandbox_kernel/framing.py` | relay、`executor_client`、`executor_server`／`executor_session` |
| proxy 端的 relay 與它的失敗代碼 | `sandbox_kernel/relay.py`（`read_token`）、`spec.RELAY_FAILURE_CODES` | `ExecutorRunner`、`executor_client`、monitor 的 `report_failure` |
| 可信直譯器、狀態 fd 的編碼與解析、`net_strict`、封鎖埠上限與固定埠、執行器 env 允許清單 | `sandbox_kernel/spec.py`（`trusted_interpreter`、`owner_chain_issue`、`encode_status`／`parse_status`、`MAX_BLOCK_PORTS`、`DEFAULT_BLOCK_PORTS`、`EXECUTOR_ENV_ALLOWED`、`EXECUTOR_GIT_CONFIG`、`filter_executor_env`、`executor_env_issue`） | 啟動器、relay、`runners`、`status`、`profiles`、執行器伺服器 |
| 擁有者位元的路徑鏈修復；執行器自己的私有 inode（W6）另外拿掉群組與其他人的位元 | `sandbox_kernel/owner_repair.py` `repair_chain`、`repair_private`（`owner_mode`、`private_mode`） | `services/isolation/repair.py`（只用 `repair_chain`）、執行器伺服器（兩者） |
| 執行器伺服器（啟動、START 驗證、規則集快取、subreaper、伺服器端自癒）與它的程式清單 | `services/isolation/executor_server.py`（`EXECUTOR_CODE_FILES`、`executor_code_digest`、`config_problem`）＋`executor_session.py`（`KIND_LAYOUT`、START 上限） | `touka-executor` 的 command；monitor 的 `server_digest`；`tests/lint/test_executor_server_imports.py` |
| READY 的格式與逐項核對、健康檢查、執行器應掛的 roots | `services/isolation/executor_client.py`（`parse_ready`、`ready_issue`、`check_health`）、`status.expected_executor_roots` | `IsolationMonitor`、伺服器端的合約測試、compose 測試 |
| 封鎖埠 B 的組成（核心 B、Settings 上游清單、非公網判斷、`connections.json` 與 registry 的讀法）；實際的 B | `services/isolation/block_ports.py`（`core_block_ports`、`SETTINGS_UPSTREAM_URLS`、`UNBLOCKABLE_PORTS`、`is_non_public_host`、`read_connection_ports`、`read_registry_ports`）；`isolation_monitor.block_ports(location)` | profile、健康檢查、卡片 |
| 路由快照（設定裡全部連線＋目前路由，唯讀）與寫檔後的重讀 | `ModelManager.route_endpoints()`、`reload_connections()`（`main.py` 以 `isolation_monitor.add_upstream_source` 接上） | monitor；連線存檔、設定快照匯入、啟動補檔、系統還原 |
| 執行器範圍、降級去處、卡片欄位與 ack | `status.executor_scope_issue`／`fallback_for`；`consent.Fallback`（含 `executor`）與 `PolicyRecord.executor_eligible`；`approval_flow.CardIsolation`（`expected`、`net`、`block_ports_issue`、`local_issue`） | `runners`、`approval_flow`、ToukaChat 卡片 |
| 權限自癒的裝飾器、豁免與掛鉤注入 | `utils/permission_repair.py`（`permission_repair`、`permission_repair_exempt`、`set_permission_repair`） | `resource_manager.py` 每個碰檔案的公開方法（`tests/lint/test_resource_manager_permission_repair.py` 釘選） |
| 自癒白名單、保護層、頻率限制與計數 | `services/isolation/repair.py`（`PermissionRepairService`、`install`） | monitor（`ensure_ready` 安裝、定期修根、`_state_issue`）、`GET /isolation` 的 `repairs` |
| 隔離狀態端點與通用提示代碼 | `routers/observability.py`（`GET /isolation`、`SYSTEM_NOTICE_*`、`collect_system_notices`）＋`models/observability.py`（`IsolationStatusResponse`、`SystemNotice`） | ToukaChat `useIsolationStatus`、`SystemStatusContext` |
| 卡片與橫幅的文案規則、原因說明的翻譯位置 | ToukaChat `lib/isolation_wording.ts`、`hooks/useIsolationReasonText.ts`；翻譯 `chat.tool_approval_card.isolation.*`、`sidebar.isolation_banner.*`、`sidebar.system_notice.*` | `IsolationNotice`、`IsolationBanner`、`SystemNoticeBar` |
| 設定讀不到時的健康度 | `autonomy_service.config_health`（`autonomy_config_unreadable`）、`permission_service.permission_config_health`（`auth_policies_unreadable`）；MCP 寫入端 `mcp_hub_service._read_json_or_raise` | `/health` 的 `status` 與 `system_notices` |
| 隔離邊界不碰完整環境 | `tests/lint/test_isolation_boundary_no_full_env.py`（`child_python_env`、`get_full_env`、`get_clean_env`）；`utils/child_env.py` 的說明 | `sandbox_kernel/**`、`services/isolation/**` |
| 執行器容器、ipc 與 state volume | 三份 compose 的 `touka-executor`、volume `touka-exec-ipc`、`touka-executor-state`、網路 `touka-exec-net`；Dockerfile 預建 `/app/ipc/executor`、`/app/executor_state` | `test_executor_compose_security.py` 釘住 |
| Linux 隔離 CI 的判定 | `.github/workflows/isolation.yml`＋`scripts/ci/isolation_junit_check.py` | GitHub Actions |

## C 線第二批第一段：代理人指令的隔離（2026-10-15，ADR-0272）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 隔離層級的值與正規化（缺值、看不懂當 `none`）、要隔離的工具、已接上 runner 的動作、要隔離卻沒接 runner 的工具（審批卡依工具標「未隔離」）、feature 對照、Touka 目錄底下不能授權的頂層名稱（和 `git ls-files`、`.gitignore` 雙向同步） | `ToukaProxy/src/utils/isolation_policy.py`（`ISOLATION_REQUIRED_TOOLS`、`ISOLATION_WIRED_ACTIONS`、`ISOLATION_UNWIRED_TOOLS`、`RUNNER_NOT_WIRED`、`normalize_isolation`、`landlock_features`、`TOUKA_TOPLEVEL_PROTECTED`） | `models/safety.py`、`permission_service`、`approval_flow`、`tools/terminal.py`、`services/isolation/*`、`utils/touka_dirs.py` |
| Landlock ABI 門檻（`MIN_ABI` 3、`MIN_NET_ABI` 4、`VERIFIED_ABI_MAX` 7、`effective_abi`） | `ToukaProxy/src/sandbox_kernel/landlock.py`（`isolation_policy` 以 `EXECUTOR_LAYER_MIN_ABI`、`LANDLOCK_LAYER_MIN_ABI` 別名匯入，不另存） | 啟動器、`isolation_policy`、`status` |
| 啟動器 spec 的版本、欄位（含 `parent_pid`）、上限、九個公開設定檔名、失敗標記與代碼、自測的網路設定代碼（也是卡片原因）、kernel 雜湊、自測輸出解析 | `ToukaProxy/src/sandbox_kernel/spec.py`（`SPEC_VERSION`、`parse_spec`、`PUBLIC_CONFIG_NAMES`、`FAILURE_CODES`、`NET_SYSCTL_CODES`、`kernel_digest`、`parse_selftest_output`） | `launcher.py`、`services/isolation/{profiles,runners,status}` |
| 唯一能 exec 的地方（只用標準函式庫、`python -I -S`） | `ToukaProxy/src/sandbox_kernel/launcher.py`；容器內只從 `TOUKA_SANDBOX_KERNEL_DIR`（映像 `/opt/touka-sandbox`）載入 | `LandlockRunner`、`IsolationMonitor` 自測；`tests/lint/test_security_kernel_ratchet.py` 執法 |
| 層級判定（`local_landlock` 與 `level`、時序、重探） | `services/isolation/status.py` `isolation_monitor`（`IsolationMonitor`：`snapshot`、`ensure_ready`、`level_for`、`fallback_for`、`report_failure`；`CHECK_FAILED` 只在這裡定義） | `policy`、`runners`、`agent_command`、`approval_flow`、`auto_reviewer`、`permission_service` |
| 政策當下的層級與專案根（只來自 ctx／session，不讀模型參數；只看內建工具）、審查員看的層級 | `services/isolation/policy.py`（`policy_level`、`session_project_root`、`reviewer_level`） | `tool_executor`、`tools/terminal.py`、`approval_flow.CommandApprover` |
| 未隔離同意（subject、一次性、10 分鐘、上限 4,096） | `services/isolation/consent.py` `consent_ledger`（`consent_subject`、`ConsentKey.of`） | `policy`、`approval_flow`、`agent_command`、`evolution_command` |
| 選 runner 與「降級要先經人核准」 | `services/isolation/runners.py` `runner_broker`（`RunnerBroker.acquire`、`LaunchPlan.release`；固定維護指令用的 `acquire_maintenance`） | `agent_command`、`evolution_command`、`home_wipe`（刪除靈魂時清家目錄） |
| profile、永不授權、封鎖埠、環境變數改寫（含 `PYTEST_ADDOPTS`） | `services/isolation/profiles.py`（`build_agent_command_profile`、`build_evolution_profile`、`authorize_derived_path`、`grantable_roots`（依身分）、`fixed_protected`、`blocked_ports`、`sandbox_env`） | `agent_command`、`evolution_command`、`status`、`approval_flow` |
| Touka 目錄偵測（往上到鏡像根、往下 3 層、資料根名稱、快取與背景續掃） | 標記與資料根名稱在 `sandbox_kernel/spec.py`（`TOUKA_MARKERS`、`DATA_ROOT_NAMES`、`fold_name`、`listing_kind`，C2-2 搬入、執行器伺服器共用）；`utils/touka_dirs.py`＋`services/isolation/project_scan.py` `project_scanner` | `profiles.touka_directory_verdict`、執行器伺服器的 roots 檢查 |
| 代理人指令與演化接上 runner | `services/isolation/agent_command.py`（`take_grant`、`command_scope`、`command_level`、`prepare_command`）、`services/isolation/evolution_command.py` | `tools/terminal.py`、`services/review/evidence.py`；`tools/builtin/evolution.py`、`evolution_security.py` |
| 審批卡的隔離標示與 `isolation_ack` | `services/approval_flow.py`（`CardIsolation`、`card_isolation`、`ISOLATION_ACK_FIELD`）；事件欄位 `utils/tool_events.py` | `ToukaChat` `stream_tree.ts`、`ToolApprovalCard.tsx`（翻譯 `chat.tool_approval_card.isolation.*`） |
| 是否在容器內（看 `/.dockerenv`） | `config.is_container_runtime` | `config.get_app_data_dir`、`profiles.current_layout` |
| 隔離暫存區（代理人的 HOME／TMP／快取；不在 data、不進備份） | named volume `touka-sandbox-state`（三份 compose）→ `TOUKA_SANDBOX_STATE_DIR`；映像預建 appuser 0700 | `profiles.state_dirs`、`status` 的健康檢查 |
| 測試替身（層級釘 `landlock`、runner 直通、每測試清帳本；`real_isolation` 退出） | `tests/conftest.py`＋`tests/support/isolation.py` | 全部 pytest；只有 `tests/integration/test_isolation_linux.py` 用真的 |

## Claude 5.5 依用途少想、送前最壞花費、價目分檔（2026-10-16，ADR-0271）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 用途→思考強度（審查員、互審刻意不列） | `ToukaProxy/src/services/completion.py` `_THINKING_PROFILES`、`thinking_profile_for`、`PURPOSE_INLINE_COMPLETION` | `complete_once` 的 `_adapter_context`；`routers/proxy.py` |
| 思考強度的鍵與型別（寫：proxy.py、completion；讀：只有 ClaudeAdapter，靜態測試釘住） | `handlers/adapters/base.py` `THINKING_PROFILE_KEY`、`ThinkingProfile` | `ClaudeAdapter.prepare_request` |
| 用途標頭白名單 | `routers/proxy.py` `PURPOSE_HEADER`、`_CLIENT_PURPOSES`、`_client_thinking_profile` | `_apply_chat_request_context`（兩個聊天端點） |
| 型號收的 effort 與關思考方式（5.5 三列；5.6 以後照舊） | `services/models/model_rules.py` `CLAUDE_EFFORT_LEVELS`、`THINKING_OFF_MAX_EFFORT`、`ModelRules.effort_levels`／`thinking_off_mode`、`claude_thinking_plan`、`claude_send_max_tokens(thinking_off=)` | `claude_adapter`（讀者名單不變） |
| OpenAI `stop` → Claude `stop_sequences` 過濾 | `adapters/openai_to_claude.py` `claude_stop_sequences` | `claude_adapter`（OpenAI 入口） |
| 送前最壞花費估算 | `services/cost_guard.py`（`estimate_input_tokens`、`worst_case_input_price`、`worst_case_usd`、`worst_case_call_usd`）；人工實測的 `tests/memory_eval/live_bounds.py` 是同一估法的測試側副本，parity 測試釘住 | `complete_once` |
| 送前是否超過上限 | `services/autonomy.py` `AutonomyService.would_exceed`（與 `is_over_budget` 共用 `_cap_limit`） | `complete_once` |
| 分檔計價（每請求依含快取讀寫的提示長度） | `services/providers/pricing.py` `Rate.long_prompt`、`for_prompt`、`cost_usd`、`flat_cost_usd`、`HAIKU_5_5_LONG_PROMPT_TOKENS` | `telemetry`、`cost_guard`、計分卡估算 |
| 整次 run 金額（畫面 `token_usage` 與稽核 CSV 同一個值） | `services/telemetry.py` `run_cost_usd`、`is_long_prompt`；`handlers/react_core.py` `_RunUsage.long_totals` | `react_core`、`log_usage_audit(cost_usd=)` |
| 審查員等待預設與上限 | `models/autonomy.py` `AutoReviewConfig.timeout_s`（60，`gt=0, le=120`）＋`config/autonomy.json.example`；前端 `useAutoReviewSettings` `REVIEWER_TIMEOUT_MAX_S`（對齊 `le=120`） | `auto_reviewer._config`；ToukaChat 審查員卡片 |
| 人工實測的最壞估價取貴檔 | `tests/smoke/manual/memory_eval_probe.py` `_dearest_rate` | 記憶評估工具、`thinking_latency_probe.py` |

## 記憶系統第二批：段 7 清除連備份（2026-10-15，ADR-0270）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 快照清單規則（頂層一般檔、不是點開頭、名稱不含 `:`、`.zip` 不分大小寫）與路徑種類（不跟連結） | `ToukaProxy/src/services/system_backup.py` `is_snapshot_name`、`path_kind`、`list_snapshot_files`（`strict` 給清除用）、`is_listed_snapshot` | `routers/system`（清單、統計、下載、刪除、還原）、`backup_scheduler_service` 修剪、`create_backup` 的新檔名、清除 |
| 清除時要處理的表（fail closed） | `ToukaMemory/src/memory_server/factual.py` `MEMORY_SCRUB_TABLES` | `inspect_store_image`、`scrub_store_image`、`compact_after_reset` |
| 升級與清除升級備份的互斥 | `factual._UPGRADE_BACKUP_LOCK`（`upgrade_backup_lock()`、`FactualStore._upgrade_with_backup_lock`） | 升級、`memory_scrub/upgrade_backups` |
| 清除進行中旗標 | `ToukaProxy/src/services/memory_scrub/__init__.py` `begin`、`in_progress`、`ScrubSession` | 重置協調器（`reset_in_progress`）、還原端點 |
| 重置或清除進行中 | `services/agent_reset/orchestrator.py` `reset_in_progress()` | 還原端點、第二個重置 |
| 清除的原因代碼與完成判定 | `ToukaProxy/src/models/memory_scrub.py` `ScrubReason`、`MemoryScrubReport.build`；Memory 端 `factual.ScrubFailure`（值都在 `ScrubReason` 裡） | 重置回應、結果稽核、ToukaChat 結果畫面 |
| 清除稽核的格式（一份） | `services/memory_scrub/orchestrator.py` `audit_result`、`audit_rejected`（路由經 `agent_reset.orchestrator.scrub_rejected`） | 路由、重置協調器 |
| 換檔順序、戳記、連結判斷、改名重試 | `services/memory_scrub/swap.py`（`replace_verified`、`stamp`、`folder_problem`、`RENAME_ATTEMPTS`） | 升級備份與快照清除 |
| collection 名稱、點 ID、Qdrant 本機版面常數、不分大小寫的比較 | `ToukaMemory/src/memory_server/utils/collection_names.py`（`collection_name_for`、`fact_point_id`、`names_fold_equal`、`QDRANT_*`） | `VectorStore`、`memory_collection_guard`、快照清除、線上 collection 磁碟核對 |
| collection 撞名判定（已知 ID 四個來源、改名只排除名稱來源） | `ToukaProxy/src/services/memory_collection_guard.py`（`known_memory_ids`、`colliding_ids`、`assert_id_free`、`point_ids_of`） | 一般重置、刪除前檢查、清除預檢、建立靈魂與自訂模型 |
| agents_registry 的鍵（讀不到就拒絕） | `services/agent_service.py` `registered_ids`；`utils/config_utils.config_file_exists` | 撞名判定 |
| 快照腳本的暫存名稱 | `ToukaProxy/scripts/manage_backup.py` `PARTIAL_SUFFIX`、`SNAPSHOT_WORKDIR_PREFIX`；Proxy 端同值 `memory_scrub/leftovers.P1_*`（S20 核對） | 中斷暫存清理 |
| 新錯誤碼 | `errors.json.example` 1410～1417（`MEMORY_SCRUB_*`、`SYSTEM_RESTORE_IN_PROGRESS`、`AGENT_RESET_IN_PROGRESS`、`AGENT_ID_*`）→ `sync_errors.py` | 重置、還原、建立端點；ToukaChat |
| 只有重置端點加長的反向代理逾時 | `nginx/nginx.conf.example` 重置端點的 location（30 分鐘） | 部署 |

## C 線安全修補 h2：這一輪列給模型的工具（2026-10-15，ADR-0269）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 這一輪實際列給模型的工具（原生工具名；MCP 的（伺服器, 裸工具名）；不可變、不做 I/O） | `ToukaProxy/src/models/advertised_tools.py` `AdvertisedTools`（`from_schemas`、`permits_native`、`permits_mcp`、`has_server`）；存在 `PipelineContext.advertised_tools`（`models/context.py`，`InstanceOf[AdvertisedTools] \| None`） | `services/tool_executor.py` `ToolExecutor._offer_denial`（只讀） |
| 誰能寫這份清單（結構測試 C3 釘住） | 正式程式只有兩處：`middleware/layers/tool.py` `ToolLayer`（由 `_inject_tools` 回傳的實際注入、去重後 schema 算，一律覆寫）、`tools/subagent_tools.py` `_run_subagent`（子代理自己組裝的 `request.tools`）；測試例外只有 `tests/tool_eval/live_episode.py` | 所有經 `run_react_loop` 的模型迴圈 |
| 舊名工具 → 後繼正式工具（後繼全部列出才放行；鍵＝Guard #45 基準的 `legacy_tools`） | `ToukaProxy/src/tools/builtin/__init__.py` `LEGACY_ADVERTISED_SUCCESSORS`（`MappingProxyType`） | `AdvertisedTools.permits_native` ← `ToolExecutor._offer_denial` |
| 執行器要不要核對清單（只能用名稱指定、建立後唯讀；正式程式只有兩個建立點、各一次，結構測試 C2 逐一比對建立點，並限定只有這兩個檔能匯入 `tool_executor` 模組） | `ToolExecutor(..., *, enforce_advertised)`：`handlers/react_core.py` `run_react_loop` 寫 `True` 並在建好後再檢查一次（沒開就 `SYSTEM_ERROR`）；`routers/debug.py` `debug_execute_tool` 寫 `False` | 模型迴圈；人按的 `/debug/execute` |
| 子代理一定拿掉的工具（請求與清單同步） | `tools/subagent_tools.py` `_SUBAGENT_STRIPPED_TOOLS`（`dispatch_subagent`、`dispatch_subagent_group`） | `_assemble_subagent_request`、`_run_subagent` |

## 記憶系統第二批：段 4 可信活動時間軸與時間感（2026-10-15，ADR-0267）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 可信活動事件表（只有中繼資料、trust 寫死 `'system'`、epoch 毫秒）的結構與讀寫 | 建表 `ToukaProxy/src/utils/async_db.py` `init_db_async`；讀寫 `ToukaProxy/src/utils/continuity_events_db.py`（`EventRow`、`insert_event`、`read_snapshot`、`count_agent_events`、`purge_agent_events`、`list_agent_events`） | 記錄器、continuity、重置、刪除前檢查、GDPR 匯出 |
| 可信紀錄起點 | `system_settings` 的 `trusted_timeline_epoch`（`async_db.TRUSTED_TIMELINE_EPOCH_KEY`，第一次建表時寫）與 `trusted_timeline_epoch:<靈魂>`（`continuity_events_db.agent_epoch_key`，重置時寫） | continuity 的起點行 |
| 事件種類、event_id 規則、模型寫的 id 怎麼顯示、誰的事件不記 | `ToukaProxy/src/services/continuity_events.py`（`EVENT_KINDS`、`REPEATABLE_KINDS`、`event_id`、`safe_ref`、`events_scope`、`record_user_turn`、`record_*`） | SoulLayer、心跳迴圈、`mission_tools`、記憶閘道、continuity |
| 唯一的寫入入口（同步、不阻塞、照呼叫順序逐筆寫） | `continuity_events.ContinuityEventRecorder`（`main.py` 啟動時 `start_recording`、關機時 `stop_recording`）；Memory 端只認 `memory_server.protocols.IContinuityEventRecorder`，經 `memory_server/services/event_recorder.py` 登記 | 上面所有掛點；`MemoryManager.save_memory`／`resolve_duplicate`、`lifecycle.supersede_memory` |

## 記憶系統第二批：段 T4 對話時間戳、摘要時間範圍、舊對話直接歸檔（2026-10-15，ADR-0264）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 對話樹節點的時間（前端蓋、毫秒、已有的不動）與秒／毫秒統一 | `ToukaChat/src/lib/stream_tree.ts`（`buildUserTurnTree` 的 `timestamp`、`stampNodeTime`）；`ToukaChat/src/lib/time_utils.ts` `toEpochMs` | `StreamRegistryProvider`（送出時間只取一次、`persistDone` 蓋完成時間）、`export-utils.formatToOpenWebUI` |
| 對話紀錄時間給模型看時的來源說明 | `ToukaProxy/src/utils/time_utils.py` `CONVERSATION_RECORD_TIME_NOTE` | `continuity_service._previous_turn_line`（對話樹那條才加）、`dialogue_summarizer.summary_time_range` |
| 前情摘要的時間範圍行與送摘要員的每行時間 | `ToukaProxy/src/services/dialogue_summarizer.py`（`summary_time_range`、`_format_dialogue`、`_SYSTEM`） | `summarize_dialogue` ← `SlidingWindowService` 剪裁（寫進 `context_summary`） |
| 系統把對話歸檔進舊對話庫（`legacy/` 不寫檔、不搬檔；訊息只新增） | `ToukaMemory/src/memory_server/services/legacy_sync.py` `LegacySyncService.archive_payload` → `legacy_store.py` `LegacyStore.archive`（一個交易） | 滑動視窗剪裁 ACID-A、`POST /v1/ui/sessions/legacy/export`、`sync_all_json_to_db`；兩個 tools 目錄不得引用（`test_legacy_archive.py` 釘住） |
| 手動同步的已處理紀錄（檔名, 內容 SHA-256） | 舊對話庫表 `processed_files`（`LegacyStore.processed_keys`、`LegacySyncService.file_status`） | `sync_all_json_to_db`（重跑跳過）、`GET /v1/stats/legacy/{agent_id}`（`archived_files`／`pending_files`） |
| 聊天側剪裁的身分（家對話所屬的靈魂，不用請求者） | `ToukaProxy/src/services/dispatcher.py` `_home_soul_identity`（`home_for_agent`；判定與設家相同，身分建法與心跳相同） | `_maybe_trim_after_chat` |
| 在訊息內容尾端附一段文字（多模態 JSON 字串先還原成 list） | `ToukaProxy/src/adapters/content_normalize.py` `append_text_part` | `continuity_service._append_to_last_user`、`ContextLayer._append_context`（`<ide_context>`） |

## 記憶系統第二批：段 L 記憶會演變與安全標記（2026-10-15，ADR-0263）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 事實的生命週期狀態值 | `ToukaMemory/src/memory_server/factual.py` `FactStatus`（目前只有 `superseded`；存在 v2 的 `status` 欄，連結在 `superseded_by`） | `FactualStore.supersede_fact`／`superseded_history` |
| 新記憶的寫入檢查（save 與 supersede 共用：消毒、安全標記、完全相同查重、語意查重）、一步取代、查被取代前的版本 | `ToukaMemory/src/memory_server/services/lifecycle.py`（`check_new_memory`、`supersede_memory`、`describe_history`、`flagged_last`；常數 `DEFAULT_DUPLICATE_SIMILARITY_THRESHOLD`、`DUPLICATE_SIMILARITY_THRESHOLDS`（以嵌入模型家族為鍵，`duplicate_similarity_threshold()` 查表，ADR-0265）、`DUPLICATE_CANDIDATES_PER_FACT`、`MEMORY_HISTORY_MAX_DEPTH`） | `MemoryManager.save_memory`／`query_memory`／`find_duplicates`；`tools/diag.manage_duplicate_memories` ← `memory_tools(action="manage_dups", operation="supersede")` |
| 記憶的安全標記（偵測與名稱；只標記、不改內容） | `ToukaMemory/src/memory_server/utils/content_guard.py`（`memory_safety_flags`、`SAFETY_FLAG_PROMPT_INJECTION`、`SAFETY_FLAG_UNREADABLE`；存在 v2 的 `safety_flags` 欄，不是 NULL 就算有標記） | `lifecycle.check_new_memory`、`FactualStore._row_to_fact`／`query_facts(exclude_flagged)`／`count_flagged_facts`、`tools/recall.random_recall`、`tools/diag.memory_diagnostics` |
| 封存時向量沒同步的標記與補上 | `ToukaMemory/src/memory_server/services/index_state.py`（`archive_in_index`、`reindex_facts`） | `MemoryManager.resolve_duplicate`、`lifecycle.supersede_memory`、`memory_tools(action="reindex")` |

## 記憶系統第二批：段 6 系統快照、刪除靈魂前檢查（2026-10-15，ADR-0262）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 快照／還原腳本的位置 | `ToukaProxy/src/services/system_backup.py` `backup_script()`（`ToukaProxy/scripts/manage_backup.py`） | `create_backup`、`routers/system.run_restore` |
| 系統快照目錄（`.env` 所在資料夾的 `backups`） | `system_backup.backup_dir()` | 快照輸出、`routers/system._get_backup_dir`（清單、統計、下載、刪除、還原）、`backup_scheduler_service.backups_dir`（排程清理）；三個名字指同一個函式 |
| 交給快照／還原腳本的路徑（資料根、`.env`、設定目錄、輸出目錄） | `system_backup.script_path_args()` | `create_backup`、`routers/system._execute_restore` |
| Python 子程序環境（PYTHONPATH＝PROXY_ROOT、`ToukaMemory/src`、原本的值，去重保序） | `ToukaProxy/src/utils/child_env.py` `child_python_env()` | `DispatchSupervisor.build_child_env`、`create_backup`、`_execute_restore` |
| 執行中 SQLite 的一致複本 | `ToukaMemory/src/memory_server/factual.py` `FactualStore.copy_sqlite_snapshot` | `manage_backup.py`（`core_memory.db`、Qdrant 各 `storage.sqlite`） |
| 刪除靈魂前「還有記憶」的判斷 | `ToukaProxy/src/services/agent_reset/delete_guard.py` `assert_memory_cleared`（`count_agent_memory`＋`VectorStore.has_agent_collection`） | `routers/souls.delete_soul` |
| 新錯誤碼 | `errors.json.example` `SOUL_MEMORY_NOT_RESET`（1408／409）、`SOUL_MEMORY_CHECK_FAILED`（1409／503）→ `sync_errors.py` | 刪除靈魂端點、ToukaChat SoulStudio toast |

## 記憶系統第二批：段 T1 舊對話時間、P28 撤回（2026-10-15，ADR-0258、ADR-0259）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 秒／毫秒換算（小於 1e11 視為秒）、給模型看的台北時間格式（標 `Asia/Taipei`）、`YYYY-MM-DD` 台北日界、秒範圍換毫秒 | `ToukaMemory/src/memory_server/utils/legacy_time.py`（`SECONDS_CUTOFF`、`to_epoch_ms`、`format_taipei`、`taipei_day_start`、`seconds_range_to_ms`；純函式、不取現在時間） | `services/legacy_store`（寫入、查詢與排序）、`services/legacy_sync`、`tools/legacy_vault.search_conversations`、`tools/recall.random_recall`、`services/manager.normalize_time_range`／`query_memory` |
| 對話是否自動刪除 | 不自動刪除（P28 撤回；沒有 TTL 設定、沒有清除函式） | 刪除只經人類手動刪除或靈魂重置；`tests/unit/test_sec_batch_epsilon.py` 釘住 |

## 記憶系統第一批：段 6 現在時間區塊、歷史資料界線（2026-10-15，ADR-0257）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| `<continuity>` 區塊（現在時間、距這個對話上一輪多久、任務看板時間）的 tag、組字與記憶體紀錄上限 | tag `ToukaProxy/src/services/prompt_blocks.py` `CONTINUITY`（列在 `ALL_BLOCK_TAGS`，不列 `VOLATILE_SYSTEM_TAGS`）；組字 `ToukaProxy/src/services/continuity_service.py`（`build_continuity_block`、`inject_continuity`、`HEADER`、`elapsed_seconds`、`format_elapsed`、`last_backend_timestamp_ms`、`MAX_TRACKED_SESSIONS`） | `middleware/layers/soul.py` `SoulLayer`（靈魂路，`session_bound` 或無痕）；`context_snapshot_service.BLOCK_META`〔ADR-0267：v1 另讀活動事件，心跳由 `routers/heartbeat._execute_single_pulse` 也注入〕 |
| 只在這一次請求附在最後一則 user 訊息尾端、不進下一輪歷史的區塊 | `ToukaProxy/src/services/prompt_blocks.py` `REQUEST_TAIL_TAGS`（`IDE_CONTEXT`、`CONTINUITY`） | `adapters/openai_to_claude.py` `convert_openai_to_anthropic` 的斷點 2（`_mark_history_breakpoint`、`_split_request_tail`：Claude 歷史快取斷點打在這些區塊之前） |
| 歷史資料界線的文字與中和規則（安全，禁令 18、20） | `ToukaMemory/src/memory_server/utils/historical_data.py`（`HISTORICAL_DATA_CLOSE`、`historical_data_open`、`neutralize_historical_tags`、`wrap_historical_data`） | `MemoryManager.query_memory`、`tools/recall.random_recall`、`SoulService.inject_context_summary`、`SoulService.inject_home_messages`（`_mark_home_history`）；Proxy 從這裡匯入 |

## 記憶系統第一批：段 2～4 記憶庫升級、事實庫是正本、記憶來源（2026-10-15，ADR-0256）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 記憶庫結構版本、升級備份的位置與份數、結構狀態 | `ToukaMemory/src/memory_server/factual.py`（`FACTS_SCHEMA_STEPS` ↔ `PRAGMA user_version`；`FACTS_BACKUP_DIR_NAME`、`FACTS_BACKUP_KEEP`、`FACTS_BACKUP_SPACE_FACTOR`、`FACTS_SCHEMA_LOCK_TIMEOUT_S`、`FACTS_STATUS_RECHECK_TIMEOUT_S`；`FactsSchemaStatus`） | `FactualStore.shared()` ← `main._prepare_memory_store`（啟動時升級）；`services/reembed.open_factual_store_for_reembed`（不升級）；`routers/observability.memory_schema_notices`；重置保留清單 `agent_reset/primitives.RETAINED_STORES` |
| 事實在語意索引裡的狀態值 | `ToukaMemory/src/memory_server/factual.py` `EmbeddingState`（`unverified`／`ready`／`failed`，存在 v1 的 `embedding_state` 欄） | `services/index_state`、`FactualStore.count_embedding_states`／`list_pending_index`／`set_embedding_state` |
| 存記憶寫索引、語意結果回查事實庫、手動補索引 | `ToukaMemory/src/memory_server/services/index_state.py`（`index_new_fact`、`live_semantic_hits`、`reindex_facts`；一次上限 `MAX_REINDEX_FACTS`） | `MemoryManager.save_memory`／`query_memory`；`tools/diag.reindex_memories` ← `memory_tools(action="reindex")` |
| 記憶來源的形狀、指紋算法與顯示文字 | `ToukaMemory/src/memory_server/models/source_ref.py`（`SourceRef`、`SourceKind`、`SourceStatus`、`content_fingerprint`、`load_sources`、`describe_sources`、`SOURCE_*` 文字；存在 v1 的 `sources` 欄） | `FactualStore.save_fact`／`_row_to_fact`、`MemoryManager.query_memory`、`MemorySourceResolver` |
| 記憶來源的記下與查回（只用可信執行資訊；先查對話樹、再查舊對話庫） | `ToukaProxy/src/services/memory_source_resolver.py` `MemorySourceResolver`（實作 `memory_server.protocols.ISourceResolver`；單例由 `src/tools/builtin/memory.py` 持有） | 記憶閘道 `memory_tools`：`save` 附 `source_context`；`query` 帶 `include_sources` 時附 `source_resolver` |
| 觸發這輪的已保存 user 節點 | `PipelineContext.source_message_id`（只由 `routers/proxy._authorize_ui_persistence` 的 UI 持久模式與 `routers/heartbeat._execute_single_pulse` 寫入） | `MemorySourceResolver.capture` |
| 事實在向量庫的 point id | `ToukaMemory/src/memory_server/vector.py` `fact_point_id` | `VectorStore.save_vector`／`indexed_fact_ids`、`services/reembed.reembed_agent` |

## 記憶系統第一批：段 5 個人空間目錄（2026-10-15，ADR-0255）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 個人空間目錄（`personal_catalog`，`.touka/catalog.json`）＝衍生快取，正本是檔案系統：記哪些 metadata、只信任自己寫出的內容、隱藏與拒寫的名稱、略過的目錄與檔名、筆數與讀取上限、根目錄 `list` 的提示行 | `ToukaMemory/src/memory_server/services/personal_catalog.py`（常數 `CATALOG_DIR_NAME`、`CATALOG_FILE_NAME`、`CATALOG_HIDDEN_NAMES`、`CATALOG_SKIP_DIR_NAMES`、`CATALOG_SKIP_ROOT_DIRS`、`CATALOG_SKIP_FILE_GLOBS`、`CATALOG_MAX_ENTRIES`、`CATALOG_READ_MAX_BYTES`、`CATALOG_TITLE_MAX_CHARS`、`CATALOG_HINT`；模型 `PersonalCatalog`、`CatalogEntry`；例外 `CatalogPathDeniedError`）；掃描經資源代理 `IResourceProxy.stat_files`、`read_file_binary` | Memory `vault_tools` 的 personal 分支 → `run_vault_action`；Proxy vault 橋接層（`tools/vault_tools.py`，`CatalogPathDeniedError` → `permission_denied`） |

## C 線第一批：小件修補與執行路徑缺口（2026-10-15，ADR-0254）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| MCP stdio 子程序可以從父環境繼承哪些變數（白名單底；`PATH`、`HOME`、`SYSTEMROOT` 一類由 MCP SDK 的 `get_default_environment()` 帶；Windows 名稱不分大小寫） | `ToukaProxy/src/protocols/mcp/environment.py` `LAUNCH_ENV_ALLOWLIST`（`LaunchEnvironmentPolicy.production`／`clean` 共用） | 正式安裝的 MCP stdio 伺服器（白名單＋條目 `env`＋`TOOL_BASE_URL`）、創建引擎試跑 `services/creation/smoke.py`（白名單＋spec `env`、不解析哨兵） |
| 哪些工具的結果要做終端自我修復（`[EXIT_CODE: N]` 非 0 附提示）與重複查詢只計一次的上限 | `services/tool_executor.py` `TERMINAL_REPAIR_TOOLS`、`MAX_TRACKED_REPAIR_RESULTS` | `ToolExecutor` 的終端修復區塊 |
| 演化沙盒 venv 的 Python 路徑（`<工具沙盒根>/.venv/Scripts/python.exe`，POSIX `bin/python`） | `tools/builtin/evolution.py` `_venv_python`（`_VENV_DIR`） | 建 venv、pip 安裝、`verify_mcp_locally`、`request_mcp_creation` 寫進 `server_config.json` 的 `command` |
| 演化與系統工具的 MCP 設定檔名 | `utils/config_utils.py` `MCP_CONFIG_FILENAME` | `tools/builtin/evolution.py`、`tools/builtin/system.py`、`services/isolation/evolution_command.py` |
| 可當基礎／主要模型的一般模型（前端下拉；不列 `is_agent`、`owned_by` 是 human 或 touka） | `ToukaChat/src/lib/default_model.ts` `isSelectableBaseModel` | `ModelStudio` 基礎引擎下拉、`SoulStudio` 主要模型下拉 |
| 最後一次還原的結果（`idle`／`running`／`failed`＋錯誤碼，只在記憶體） | `routers/system.py` `_last_restore`／`_record_restore`；模型 `models/system_restore.py` `RestoreStatus` | `GET /v1/system/restore/status`；ToukaChat `hooks/useRestoreWatch.ts`（`EnvSettings`） |

## 記憶系統第一批 PR-1（2026-10-15，ADR-0253）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 記憶提示的代碼與等級（只給畫面看，不影響模型） | `ToukaProxy/src/routers/observability.py` 的 `MEMORY_NOTICE_*` 常數（等級 `MEMORY_NOTICE_LEVEL_INFO`、`MEMORY_NOTICE_LEVEL_WARNING`；代碼 `MEMORY_NOTICE_UNMIGRATED_LEGACY_FACTS`）；產生函式 `collect_memory_notices`。記憶庫結構提示由 `memory_schema_notices`（`_SCHEMA_STATUS_NOTICES`＋`MEMORY_NOTICE_NEW_STORE_BESIDE_BACKUPS`）每次請求即時算，狀態來源 `FactualStore.existing_shared()`（ADR-0256） | `main._lifespan_runtime`（啟動算一次存進 `app.state.context`）、`GET /v1/memory` 的 `memory_notices`；前端代碼→翻譯 key 對照在 `ToukaChat/src/components/sidebar/MemoryNoticeBar.tsx` `NOTICE_MESSAGE_KEYS` |
| 前端「記憶提示裡有沒有警告級」 | `ToukaChat/src/lib/memoryNotices.ts` `hasMemoryWarning` | `LeftSidebar` → `SidebarIdentity` 的警告標記 |
| 前端記憶提示的解析（選填、格式不對略過、看不懂的等級當資訊級） | `ToukaChat/src/lib/SystemStatusContext.tsx`（`MemoryNotice` 型別、`parseMemoryNotices`） | `RightSidebar` 的 `MemoryNoticeBar`、`LeftSidebar` |
| 哪些靈魂有沒搬入的舊版事實檔（只看檔名） | `ToukaMemory/src/memory_server/factual.py` `find_unmigrated_jsonl_agents`（`LEGACY_FACTS_JSONL_PATTERN`） | `collect_memory_notices`、`tools/diag.memory_diagnostics` |
| Memory 模型看不到的內部 action | `ToukaMemory/src/memory_server/tools/contracts.py` `MEMORY_INTERNAL_ACTIONS` | Guard #45 以 AST 讀取（`assigned_string_set`）；確切清單由 `test_native_contract_invariants.py::test_memory_internal_actions_exact` 釘住 |
| 重複記憶掃描的上限與門檻 | `ToukaMemory/src/memory_server/services/manager.py`（`MAX_DUPLICATE_SCAN_FACTS`）；相似度門檻 `duplicate_similarity_threshold()`（依嵌入模型家族查表，ADR-0265）與候選數 `DUPLICATE_CANDIDATES_PER_FACT` 在 `services/lifecycle.py`（ADR-0263） | `MemoryManager.find_duplicates` ← `manage_dups` 的 scan；`MemoryManager.save_memory` 的語意查重（ADR-0256） |
| 嵌入模型家族（要不要加 e5 的 `query:`／`passage:` 前綴、用哪組門檻） | `ToukaMemory/src/memory_server/vector.py` `embedding_family()`（ADR-0265） | `_format_for_embedding`、`query_score_floor`、`lifecycle.duplicate_similarity_threshold` |
| 語意查詢的相似度下限 | `ToukaMemory/src/memory_server/vector.py`（`DEFAULT_QUERY_SCORE_FLOOR`、`QUERY_SCORE_FLOORS`、`query_score_floor()`；ADR-0265） | `VectorStore.query_vectors`（`score_threshold` 沒給時） |
| 舊對話目錄的解析 | `MemoryManager.get_legacy_dir(identity, resource_proxy=…)`（資源代理的 `get_vault_path` 解析得到時只回路徑、不建目錄） | `LegacySyncService._legacy_dir`（`get_store`、`sync_all_json_to_db`、`archive_payload`、`file_status`） |

## Touka 開發環境整理（2026-10-14，ADR-0251）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 三把自動持久化金鑰（`HEARTBEAT_TOKEN`、`TOUKA_AUTH_SECRET`、`DISPATCH_TOKEN`）歷來公開過的值與拒用判斷（比對鍵名＋值、去掉前後空白；公開值先沿用 `.env` 已存的非公開值，否則交給產生並寫回） | `ToukaProxy/src/config.py`（`PUBLIC_SECRET_VALUES`、`is_public_secret`、`usable_secret`） | 四個模組：`config._resolve_heartbeat_token`、`core/security.py` `AuthSecretManager.get_secret`、`services/dispatch_supervisor.py` `DispatchSupervisor.build_child_env`、`services/setup_service.py` `SetupService._is_dispatch_token_default` 與 `SetupService.apply`（`_reject_public_secret`，ADR-0254）；範本檢查 `test_sec_batch_delta.py::test_env_example_has_no_real_secrets` |
| pytest 刪沙盒前要先釋放的東西與順序（等稽核寫入 → 關 Qdrant client → 關 SQLite → 關 log handler） | `tests/conftest.py` `SANDBOX_RELEASE_STEPS`；刪除與殘留回報 `tests/sandbox_cleanup.py` `remove_session_sandbox` | conftest 的三個收尾點（`mkdtemp` 後的 atexit、session fixture、`pytest_sessionfinish`）；新的 handle 來源加在清單裡 |
| 可以不問人直接刪的建置產物（7 行，從 repo 根算起） | 根目錄 `rebuildable-outputs.txt`（格式以開發流程的清理檢查為準，由 `tests/unit/test_rebuildable_outputs_declaration.py` 先檢查） | 開發流程的清理檢查（只讀 `origin/HEAD` 指到的預設分支上的這個檔） |

## Touka 待排小任務（2026-10-14，ADR-0250）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 靈魂存檔要不要換主要模型（人類、有指定、改了值或還沒有主要模型） | `utils/brain_persistence.py`（`should_set_active_brain`、`normalize_model_choice`） | `routers/souls.update_soul`；建立靈魂 `routers/souls.create_soul` 也用 `normalize_model_choice`（ADR-0254） |
| 哪些模型名能當靈魂的主要模型（啟用中的自訂連線，或模型清單裡非代理、`owned_by` 不是 human／touka 的模型） | `services/dispatchers/route_family.py` `is_selectable_brain`（只用 `RouteCatalog` 的 `resolve_custom_route`、`get_models`） | `routers/souls.update_soul`、`routers/souls.create_soul`（ADR-0254）；畫面下拉用同一組條件過濾（`isSelectableBaseModel`：`SoulStudio`、`ModelStudio`） |
| 自訂連線模型名查表（先完全相同、不分大小寫只有一個才用、兩個以上 409、停用照舊；`CustomRouteResolution.original_name` 帶原名） | `services/models/manager.py` `ModelManager.resolve_custom_route`（唯一入口） | `route_family()`、`resolve_model_route()`、`is_selectable_brain`、`request_model_name` |
| 靈魂存的模型名 → 請求用的名字（自訂連線回原名、其他轉小寫、模糊丟 409） | `ModelManager.request_model_name` | `middleware/layers/soul.py`、`services/dispatchers/group.py`、`tools/subagent_tools.py`、`routers/heartbeat.py` 脈衝 |
| 模型名只差大小寫無法判斷 | `errors.json.example` `MODEL_NAME_AMBIGUOUS`（1220／409）→ `sync_errors.py`（ToukaChat `error_codes.ts`、ToukaHeartbeat `error_codes.py`；兩語翻譯 `error.model.name_ambiguous`） | `resolve_custom_route`；靈魂存檔、聊天、群聊、心跳 |
| 自訂模型新 ID 撞名（和靈魂 ID 或其他自訂模型不分大小寫相同） | `routers/custom_models.py` `_validate_new_model_id`（`_names_a_soul` 比對靈魂 ID 清單，不靠檔案系統） | 建立與改名 |
| 前端預設的一般模型（第一個啟用、沒隱藏、非代理、不離線） | `ToukaChat/src/lib/default_model.ts` `pickDefaultModel` | `ModelProvider`（清單載入後的初選）、`ChatProvider`（新對話離開靈魂時） |
| openapi 合併（純函式，不載入產品程式） | `scripts/openapi_merge.py`（`merge_schemas(..., transport_models=)`、`OpenAPIMergeConflict`）；沙盒入口 `scripts/update_openapi.py` | `update_openapi.py`（`update_docs.py` 經它）、`test_api_schema_sync.py`、`test_heartbeat_models_sync.py`、`test_session_persistence_contract.py` |
| 逾時後殺行程樹並有界回收、取消往上拋 | `services/process_registry.py` `reap_process_tree`（`reap_cancelled_process` 包它並吞掉取消） | `tools/terminal.py`、`services/review/evidence.py` 的逾時分支 |
| knip 的分析範圍（不靠 `.gitignore`；產生檔與建置產物移出相依圖） | `ToukaChat/knip.config.ts` 的 `project` 排除樣式＋`package.json` `guard:prune` 的 `--no-gitignore`；被 gitignore 的路徑逐條分類在 `tests/unit/test_knip_worktree_parity.py` | Guard #20 |

## 模型溝通層第 4a 段：工具邊界插話（2026-10-13，ADR-0249）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 插話佇列（鍵 (session_id, stream_id)、10 則／4,000 字／TTL 600 秒、狀態流轉） | `services/interjection_queue.py`（`InterjectionQueue` Protocol、`InMemoryInterjectionQueue`、`interjection_queue` 單例） | 插話端點、`StreamBufferService`（串流結束、換代時清空）、`react_core`（經 `handlers/interjections.py` 取出）、`dispatcher._persist_ui_assistant_turn` |
| 插話 SSE 事件名稱與欄位（雙端） | 後端 `services/interjection_queue.py`（`INTERJECTION_DELIVERED_EVENT`、`INTERJECTION_UNDELIVERED_EVENT`、產生函式）＋前端 `lib/chat/interjections.ts`（同名常數），跨端測試 `test_interjection_sse_contract.py` | `handlers/interjections.py`；`connections.ts` 白名單、`interjectionStore` |
| 工具步驟之間送給模型的範本句 | `handlers/adapters/base.py`（`INTERJECTION_TEMPLATE`、`interjection_text`） | 四家 `append_interjection` |
| 歷史重組標註（雙端） | 後端 `utils/interjection_notes.py`（`INTERJECTION_NOTE_TEMPLATE`、`with_interjection_notes`、`annotate_node`）＋前端 `lib/chat/interjections.ts`（`INTERJECTION_NOTE_TEMPLATE`、`withInterjectionNotes`），純文字比對測試 `test_interjection_notes.py` | 前端 `buildApiHistory`；心跳脈衝、`get_home_messages`、`dialogue_summarizer` |
| 人類呼叫者閘（人類或 admin 代打） | `services/authorization_gateway.py` `require_human_requester` | 插話三端點 |

## 模型溝通層第 2c 段：三種模式、底線與審查員（2026-10-12，ADR-0246）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 硬地板類別（F1–F7）與「只有人能核准」的類別（程式常數，設定檔關不掉） | `src/utils/hard_floor.py`（`classify_hard_floor`、`HUMAN_ONLY_CATEGORIES`） | `AuthPoliciesConfig.evaluate` 最前面（Gate 2、`classify_command`、MCP MRTR、工具廣告都經過它） |
| `HUMAN_ONLY` 無人在場的錯誤 | `approval_flow.human_only_unavailable_error` | `ToolExecutor` |
| 三種模式名稱與舊值讀取對照 | `src/utils/auth_mode.py`（`AuthMode`、`AUTH_MODE_READ_MAP`、`normalize_auth_mode`） | 儲存層讀取、ToolLayer、`ui_sessions`、`debug`、`heartbeat`、MCP MRTR；前端型別來自 openapi |
| 無痕一律 Manual | `auth_mode.enforce_incognito_manual` | ToolLayer（所有模式來源之後最後套一次）、`subagent_tools._run_subagent`（子代理執行 ctx） |
| 無痕碰不到的檔案範圍（舊對話庫，讀寫都擋；ADR-0274） | `auth_mode.INCOGNITO_HIDDEN_VAULTS`、`auth_mode.incognito_hides_vault` | ToolLayer 改列 `file_verbs.INCOGNITO_FILE_VERB_SCHEMAS`；`file_verbs._scope_error`（九個動詞入口）與 Proxy `vault_tools` 橋（含舊 shim）拒絕 |
| 誰能把對話存成 Auto／YOLO | `auth_mode.authorize_auth_mode`，只由 `ui_sessions._authorized_changes`（對話存檔）呼叫 | 對話存檔；MonitorLayer 不再判斷 |
| 三模式決策表（含 Auto 的 `REVIEW` 與動態 shell `HUMAN_ONLY`） | `AuthPoliciesConfig._evaluate_rules`（`models/safety.py`）；動態 shell 偵測 `command_safety.has_dynamic_shell_syntax` | Gate 1、Gate 2、`classify_command`、MCP MRTR |
| 有沒有人在場 | `src/utils/presence.py`（`is_human_present`，只看伺服器設的 `heartbeat_run`／`subagent_run`／`dispatch_run`） | ToolExecutor、ToolLayer、MCP 互動出口 |
| 使用者原意（審查員的資料） | `src/utils/user_intent.py`（`UserIntent`） | MonitorLayer、心跳、UI 重跑、子代理 → 審查員 |
| Auto 審查員（輸入格式、判決解析、P7 計數） | `services/auto_reviewer.py`；設定 `AutoReviewConfig`（`autonomy.json` 的 `auto_review`） | ToolExecutor、互審 `policy_command_gate` |
| 審查員核准的可信欄位 | `tools/execution_context.py` 的 `_approved_by`＝`APPROVED_BY_AUTO_REVIEWER` | ToolExecutor → terminal（只滿足 `REVIEW`） |
| 心跳 `security_level` → 模式 | `routers/heartbeat._SECURITY_LEVEL_TO_AUTH` | `/awaken`、`/retrigger`；前端 `heartbeat_mode.ts` 只做顯示對照 |
| IDE「由人核准」標記格式 | Proxy `utils/ide_human_approval.py`（產生）＋ `ToukaIDE/src/mcp/humanApproval.ts`（驗證），兩邊共用測試向量 | `call_ide_tool`、IDE MCP server |

## 模型溝通層第 3b 段（2026-10-13，ADR-0248）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 串流逾時（首事件 120 秒／閒置 60 秒推測值／整輪 600 秒）、逾時文字、位元組層的「還活著」計時 | `handlers/adapters/stream_timeout.py`（`RoundClock`、`FIRST_EVENT_TIMEOUT_S`／`IDLE_TIMEOUT_S`／`ROUND_TIMEOUT_S`、`liveness_event_hooks()`） | 四家 adapter 的串流路徑；`main.py` 主迴圈 Anthropic client 與 Responses client 的 httpx2 hook；`retry.on_timeout` |
| SSE 解析與串流彙整（Chat chunk、Gemini 規則見 ADR-0248 決策 5） | `handlers/adapters/sse_stream.py`（`sse_data`、`ChatStreamAggregator`、`GeminiStreamAggregator`、`StreamShapeError`） | `openai_adapter`、`gemini_adapter` |
| 哪些錯誤可以重試、retry-after 解析 | `handlers/adapters/retry.py`（`RETRYABLE_STATUS`、`MAX_RETRY_AFTER_S`、`retry_after_s`、`on_status`、`on_timeout`） | 四家 adapter 標記；重送只在 `react_core`（`MAX_SEND_ATTEMPTS`、`RETRY_BACKOFF_S`、`_retry_sleep`） |
| 下游 finish_reason 對照與拒答／安全過濾提示 | `react_core._FINISH_REASONS`、`_REFUSAL_NOTICE`、`_SAFETY_NOTICE`（停止原因來自各 adapter 的 `stop_reason`） | 主迴圈終端 chunk |
| 沒跑完那一輪的用量估算 | `handlers/adapters/usage.py`（`estimate_partial_usage`、`PartialUsage`）；`ProviderAdapter.partial_usage` | `react_core`（取消、RoundError、重試放棄的那次）；`complete_once` 不用 |
| 已知相容廠商（才串流、才送 `include_usage`） | `services/models/model_rules.py` `COMPAT_STREAM_PROVIDERS`（看 route 的 provider） | `openai_adapter` |
| Responses 路由判定與目的地 | `route_family.resolve_model_route` 的 `responses` 種類；`config.OPENAI_RESPONSES_BASE_URL`（字串常數，不讀 `OPENAI_BASE_URL`） | `factory`（`handle_responses_request`）、`complete_once` |
| openai SDK 會讀的請求相關環境變數 | `config.py` `OPENAI_SDK_ENV_OVERRIDES`＋`strip_openai_sdk_env_overrides()`；SDK logger 層級 `pin_sdk_loggers()` | config 載入、`setup_logging`、每次建構 openai client 前 |
| Anthropic 形狀請求轉 OpenAI（含 tool_use／tool_result、list system，KF-9） | `services/dispatchers/utils.py` `convert_to_openai` | `factory` 的非 Claude 路由 |

## 模型溝通層第 3a 段（2026-10-12，ADR-0247）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 每模型送出規則（家族／版本樣式；取代第 0 段的 `claude_model_rules`；不拿來過濾模型發現，禁令 39） | `services/models/model_rules.py`（`rules_for`／`claude_rules`；Claude 非串流上限 `CLAUDE_NONSTREAMING_MAX_TOKENS`，3b 起只在主迴圈不串流送出時套用（`claude_nonstream_cap`）；context window 仍是 `context_window_registry`） | 只准 `claude_adapter`（`allows_forced_tool_choice`；ADR-0271 起思考參數經 `claude_thinking_plan`、輸出上限經 `claude_send_max_tokens`，原本是 `normalize_thinking`）、`openai_to_claude`（`-thinking`、`claude_send_max_tokens`）匯入（AST 匯入圖測試）；3b 加 `openai_adapter`、`gemini_adapter`、`responses_adapter`、`route_family`（`uses_responses_api`）；4a 起 `mid_conversation_system` 由 `claude_adapter.append_interjection` 讀取（讀取者清單見 `test_model_rules.py` 的 `_RULES_IMPORTERS`） |
| Claude API 目的地（固定常數，刻意不做成 Settings 欄位，不讀 `ANTHROPIC_BASE_URL`）；SDK 會讀的請求相關環境變數 | `config.py` `ANTHROPIC_API_BASE_URL`；`ANTHROPIC_SDK_ENV_OVERRIDES`＋`strip_anthropic_sdk_env_overrides()`（載入時與三個 SDK 建構點清除） | `main.py` 主迴圈 client、`complete_once`、`models/fetcher` 模型發現、`routers/models` 健康檢查、工具評測腳本與人工 probe；禁令 22 AST 守衛要求每個 SDK 建構點明寫 |

## 模型溝通層第 1b 段（2026-10-12，ADR-0244）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 一次性模型呼叫（單次、不帶工具、記帳、成本上限、結構化非成功） | `services/completion.py` `complete_once`（`Completer` Protocol、`CompletionResult`） | `dialogue_summarizer`、`review_loop.build_critic_completer`、`creation/model_judge`（main 注入） |
| 用量正規化四欄（未快取輸入／快取讀／快取寫／輸出） | `handlers/adapters/usage.py` `normalized_usage`／`split_included_cache` | 三家 adapter 的 `extract_usage` → `react_core` 每輪入帳、`complete_once`、`telemetry` |
| 模型名→路由＋連線資訊 | `services/dispatchers/route_family.py` `resolve_model_route`（純函式） | `dispatchers/factory.get_stream_generator`、`complete_once` |

## 模型溝通層第 0 段（2026-10-11，ADR-0241）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 不送給廠商的取樣參數名稱 | `handlers/adapters/sampling.py` `SAMPLING_PARAM_NAMES` | OpenAI 相容 adapter、跨 adapter 契約測試 |
| 模型會被送到哪一家 | `services/dispatchers/route_family.py`（`route_family`／`native_family`） | factory 兩個判斷式、`proxy_service` 同家備援 |
| 自動測試能連哪裡 | `tests/network_guard.py` | `tests/conftest.py`、`tests/smoke/lib/sandbox_env.py` |
| 備份鎖持有到備份結束 | `system_backup.run_backup_job` | 手動、匯入前、重置前、排程備份 |
| 取消時殺行程樹並有界回收 | `process_registry.reap_cancelled_process` | terminal、互審蒐證 |
| 沒設主要模型 | `errors.json.example` `AGENT_BRAIN_NOT_SET`（1215／409）→ `sync_errors.py` | `SoulLayer`、ToukaChat `useAgentBrainNotice` |

## braces 防禦回補契約（ADR-0237）

braces專案依賴與npm CLI內的brace-expansion是不同套件，不能互借PASS；Chat／IDE 依 `backport.mjs` 先 apply 再 readonly verify，Chat production runner 只驗 runtime scope。

| 責任 | 唯一來源 | 封閉契約 |
| --- | --- | --- |
| 固定回補與完整inventory | `ToukaChat/container-security/braces-backport/{backport.mjs,manifest.json,braces-3.0.3-depth-limit.patch}` | 官方3.0.3＋固定PR72 production delta；保留版本／lock URL／SRI／LICENSE，完整before/after inventory；不帶PR base無關parse變更。 |
| 安裝原件純reader | `scripts/braces_backport_evidence.py` | `touka.braces-backport.installed-bytes.v1`，sha256:前綴、sorted nodes/files arrays及每檔body_base64重算；可信source重建所有節點與parents，不信PASS或stamp。 |
| audit profile | `guard_node_cve.py --profile` | legacy `lock-only-v1` 與明示 `node-lock-and-braces-backport-v1` 兩 profile；`guard_all.py` 固定使用後者，audit source inputs 於執行期間變動即中止。 |
| 原high與修補coverage | `scripts/braces_backport_evidence.py::assess_braces_audit` | 原parser tuple、raw exit／high不改；僅固定GHSA完整via graph＋完整trusted bytes可另列covered；runtime high、critical／其他advisory／缺件仍阻擋。 |
| 安裝與工具順序 | `npm_offline_gate.py::npm_command_sequence`與固定helper CLI | full六步、runtime兩步，canonical／actual同源；full先apply再readonly verify，omit-dev只驗真runtime absence，不借full dev旗標宣稱absence。 |

本次不重現漏洞、沒有新增EXCLUDE。

## 任務／對話保存結果（可靠性 R2，2026-09-21）

| 責任 | 唯一來源 | 消費／驗證 |
|---|---|---|
| SaveResult／snapshot／SSE模型 | Proxy `models/session_persistence.py` | shared OpenAPI merge→api.ts→types.ts alias |
| strict row／revision／conditional commit | Proxy `utils/async_db.py` | SQLite原子CAS、owner／absence／generation、owned故障注入 |
| 固定ID assistant保存 | Proxy `utils/history_manager.py` | 明示UI producer，非legacy保證 |
| terminal／stream fence | Proxy `services/stream_buffer_service.py` | UI dispatcher、state／resume／stop |
| UTF-8／終態解碼 | Chat `lib/connections.ts` | live與cursor resume共用 |
| 同scope/session writer | Chat `SessionListProvider.tsx` | 純轉換、保留草稿、核對／僅重試保存 |
| 任務保存失敗 | Memory `mission_manager.py` | Proxy `mission_tools.py`安全錯誤映射 |

原session owner授權來源不變；14欄DB revision readset不等於前端可寫欄位集合。
詳 [ADR-0231](adr/0231-session-persistence-outcomes-and-revisions.md)。


## Heartbeat 交易、錯誤與安全來源（2026-09-16）

| 責任 | 唯一來源 | 消費／驗證 |
| --- | --- | --- |
| HB排程／持久模型 | `ToukaHeartbeat/models.py` | HB驗證；sync_heartbeat_models生成Proxy投影；OpenAPI→Chat |
| 設定原bytes／原子提交 | `ToukaHeartbeat/storage_service.py` | owned fault injection／舊原件保留 |
| revision／jobs協調 | `ToukaHeartbeat/schedule_service.py` | 同一transaction lock、precise job snapshot／補償 |
| token解析 | `ToukaHeartbeat/token_provider.py` | 管理／trigger／client共用；不宣稱遠端ready |
| 真人排程權限與上游 | `ToukaProxy/src/services/heartbeat_schedule_bridge.py` | human route／old-new owner／If-Match CAS |
| 出貨錯誤定義 | `ToukaProxy/config/errors.json.example` | sync_errors生成HB／Chat；Proxy既有registry缺項in-memory補齊 |
| 安全設定來源驗證 | Guard32的required examples＋fallback | 不讀取私有現場設定 |
| 出貨技能MCP宣告 | Guard37的SHIPPED_MCP_DECLARATIONS | required hub example；不glob未列宣告 |
| 前端 CI | `.github/workflows/native-tool-contracts.yml` 的 `frontend` job；最後一步從 repo 根目錄跑 ESLint 棘輪 Guard #35（ADR-0278） | 每次 PR／push 都跑，不做影響判斷；`tests/lint/test_ci_pr_checks.py` 釘住 Guard #35 那一步 |
| 後端 pyflakes CI（ADR-0254） | 同一個 workflow 的 `contracts` job：鎖定版 `ruff check --no-cache --select F --config ruff.toml ToukaProxy/src ToukaMemory/src ToukaHeartbeat ToukaDispatch` | 每次 PR／push；`tests/lint/test_ci_pr_checks.py` 釘住 |
| 完整 ruff 只減不增（ADR-0278） | `scripts/lint_ratchet.py` 收集與重拍；基準 `tests/lint/ruff_baseline.json`；`tests/lint/test_ruff_ratchet.py` 要求每條規則數量完全相等 | 在 `contracts` job 的 pytest 裡執行；改善後同一個 PR 重拍基準 |
| ToukaIDE CI（ADR-0254） | 同一個 workflow 的 `toukaide` job（Node 24.21.0、npm 11.19.1）：`install:controlled`、`test`、`typecheck`、`compile` 依序 | 每次 PR／push；`package` 只在本機；`tests/lint/test_ci_pr_checks.py` 釘住 |

詳 [ADR-0230](adr/0230-heartbeat-transaction-and-credential-contracts.md)。

## 代理人撰寫指令的政策與執行邊界（2026-10-09）

| 責任 | 唯一來源 | 使用者 |
|---|---|---|
| 指令 → ALLOW／CONFIRM／REVIEW／HUMAN_ONLY／DENY（含 C5 串接字元升級；2c 起含底線與 Auto 審查員，ADR-0246） | `PermissionService.classify_command` | `execute_command`、互審 `policy_command_gate` |
| 串接字元偵測（純函式） | `src/utils/command_safety.py` | 同上 |
| 子程序淨化環境（呼叫端 env 亦過同一道過濾） | `EvolutionPathGuard.get_clean_env`／`strip_sensitive_env` | terminal、互審蒐證、演化沙箱 |
| 子程序啟動與新行程群組 | `execution_broker.spawn_exec`／`run_sync`（ADR-0239） | 全部子程序（例外見 `tests/lint/test_security_kernel_ratchet.py`） |
| 連坐殺行程樹 | `process_registry.terminate_process_tree` | terminal、互審蒐證、dispatch supervisor |

見 SECURITY.md SP1–SP8、[ADR-0239](adr/0239-authorization-gateway-and-execution-broker.md) 與 [ADR-0137 補記](adr/0137-adversarial-review-subsystem.md)。

## 可信工具執行分類（2026-09-16）

`ToukaProxy/src/tools/execution_policy.py` 為純分類／binding 契約；`tools/builtin/__init__.py` 在可信註冊時建立 immutable callable binding。
ToolExecutor 只對同一已解析 handler 使用例外，模型參數不參與分類；Guard45 檢查有限 AST 接線與分類 metadata。
角色 grant 仍沿 `PermissionService.resolve_grants`，風險仍沿 `AuthPoliciesConfig`；本項不建立第二份權限表。見[ADR-0229](adr/0229-trusted-tool-execution-classification.md)。

## 原生工具合約 SSOT（2026-09-15）

| 責任 | 唯一來源 | 執法 |
|---|---|---|
| Memory公開輸入/action | `ToukaMemory/src/memory_server/tools/contracts.py` | schema生成＋public validation |
| 其餘六typed原生工具 | `ToukaProxy/src/tools/contracts.py` | schema生成＋executor入口 |
| trusted runtime kwargs | `ToukaProxy/src/tools/execution_context.py` | 移除模型reserved值、Context注入 |
| native角色grant | PermissionService.resolve_grants／RoleGrants.tools | ToolExecutor執行面，先於PaC/HITL |
| action風險 | `auth_policies.json.example` 的tool_risk | Guard32＋Guard45；manage_dups保守dangerous |
| 有限consumer分類 | `scripts/native_tool_contract_baseline.json` | Guard45 AST-only，非schema第二份拷貝 |

Guard45＋sandbox runtime/provider/backend測試分工及metadata更新界線見[ADR-0228](adr/0228-native-tool-public-input-contracts.md)。

本文件將 Touka 系統中分佈於各模組的「單一事實來源 (SSOT)」進行彙整。這份地圖定義了所有物理檔案、資料庫與邏輯配置的權威來源，旨在消除資料冗餘與狀態不一致。

> **驗證方法 SSOT**：[testing_guide](../guides/testing_guide.md) 明定優先現有 tests／guards／sandbox／mocks；Guard16 必要 member venv 與 Guard22/23 無憑證 audit 暫存保留。

---

## 🌍 基礎資料主權 (Data Sovereignty)

系統所有物理資料皆相對于 `AppDataDir`。

- **Docker 環境基準**: 未明示或僅給相對 `MEMORY_APP_DATA_DIR` 時使用 `/app/data`；顯式絕對覆寫保留原值，供受控 sandbox／部署定址（ADR-0209）。
- **宿主機環境基準**: 由 `.env` 中的 `MEMORY_APP_DATA_DIR` 或 `SANCTUARY_DATA_PATH` 定義 (SSOT)。
- **路徑主權隔離 (Path SSOT)**: 系統實施 **TID251 (Physical Path Ban)** 法律。嚴禁在代碼中寫死絕對路徑。
- **單一存取通道 (Resource Prison)**: 所有物理定址與 I/O 操作必須透過 `ResourceManager` 執行。嚴禁調用 Python 內建 `open()` 或 `os` 模組進行直接操作。
  - **原子寫入 (Atomic Write) ⭐ — ADR-0078**: `ResourceManager.write_file_text`（連帶 `write_json`）採原子寫入——同目錄 `tempfile.mkstemp` → 寫入 + `flush` + `os.fsync` → `os.replace`（同檔系統原子置換）；失敗清暫存並拋出。**一處改、全體 JSON 寫入者受惠**（autonomy_log / token_usage / mission / sessions-JSON / schedules），杜絕寫到一半當機導致檔案損毀。`read_json` 對 `JSONDecodeError` 記**明確 corrupt 告警**（不再靜默歸零）。
  - **資源代理依賴反轉 ⭐ — ADR-0116**: ToukaMemory 以同行程函式庫整合於 ToukaProxy（**整合模式單一部署，獨立 MCP 模式已退役**）。所有 I/O 經 `get_resource_proxy()` 工廠取得 ToukaProxy 的真實 `ResourceManager`（實作 `memory_server.protocols.IResourceProxy` 協定，含完整身分隔離與路徑保護）；取不到即 **fail-loud**，不再有 `_FallbackResourceProxy` 降級代理（其 `get_vault_path→None`／`is_protected_path→False` 等同關閉隔離與保護）。依賴反轉接縫保留以維持解耦（Guard #21 邊界守衛續存，正當性改為「解耦」）。
- **配置主權 (Env SSOT)** ⭐: 全系統環境變數與靜態配置統一收攏於 `ToukaProxy/src/config.py` (後端) 與 `ToukaChat/src/lib/config.ts` (前端)。嚴禁在業務邏輯中調用 `os.environ` 或 `process.env`。後端（含 `ToukaProxy` 與 `ToukaHeartbeat`）支援環境感知（Environment-Aware）的 `.env` 互斥載入機制，整合執行時僅加載根目錄 `.env`，獨立執行時僅加載子專案自身的 `.env`，防止配置污染與越權覆蓋。
  - **`.env` 路徑解析 SSOT（`resolve_env_file_path`）⭐ — ADR-0118（extends ADR-0052／禁令38，2026-07-02）**: 全系統對 `.env` 的讀寫路徑一律經 `src.config.resolve_env_file_path()` 解析（禁裸相對 `".env"`）。優先級：**環境覆寫 `TOUKA_ENV_FILE`（最高）** > 整合模式（根目錄 `.env`）> 獨立模式（子專案 `.env`）自動偵測。`docker-compose.yml` 設 `TOUKA_ENV_FILE=/app/host_root/.env`（**ADR-0151**：`./:/app/host_root` 恆掛 compose 目錄＝`env_file` 載入的同一 .env，與 `WORKSPACE_ROOT` **解耦**；舊制 `/app/workspace/.env` 綁 `WORKSPACE_ROOT`，指向 repo 以外即寫進沒人讀的幽靈 .env）→ 首裝精靈與自動生成的金鑰**跨容器重建持久化**並於重啟重新注入。`routers/config.py` 寫入後由 `apply_runtime_env()` 同步 `os.environ` ＋ `settings` ＋ 模組級 `src.config` 常數（**免重啟即時生效**）。
- **身分物件化定址 (Identity Anchoring)**: 全系統 Service 層、Router 層與 Resource 層嚴禁使用 `str` ID，必須統一傳遞經認證的 `AgentIdentity` 物件。系統實施「物理路徑監獄」，將代理人強制鎖定在其屬 ID 子目錄中。
- **設定與狀態隔離**: `ToukaProxy/config/` 下的 `.json` 檔案被視為「本地狀態」，不進入 Git 追蹤，具備自癒引導能力。

---

## 🛠️ 配置自癒與引導 (Self-Healing Bootstrap)

系統採用「開箱即用」的自癒機制，確保新環境部署時的穩定性：

1. **自動引導 (Bootstrap)**：啟動時由 `src/core/bootstrap.py` 檢查必需的 `.env` 與 `config/*.json`。若遺失，則從對應的 `.example` 範本恢復。設定範本 hydration 的唯一入口為 `hydrate_config_examples(source_dir, target_dir)`；production 可同目錄恢復，測試必須從 tracked source 補到 session temp target，且只補缺、不覆寫（ADR-0209）。
2. **引導隔離區 (Whitelist)**：環境變數與進程操縱權限僅限於「引導者 (Bootstrappers)」白名單檔案。
3. **範本一致性**：開發者在新增設定項時，必須同步更新 `.example` 以作為 SSOT 恢復基準。
4. **安全保護**：自癒機制**絕對不會**覆蓋已存在的本地檔案，保護使用者的私人配置。

---

## 🚑 狀態恢復指南 (State Recovery Guide)

當發生系統異常或「幽靈 Agent」現象（UI 顯示但實體不存在）時，請遵循以下恢復流程：

### 1. 數據糾偏與 ID 歸一化 (Data Normalization & ID Unification) ⭐
- **原則**：系統實施 ID 簡潔化，全面移除 `ToukaSoul` 前綴。正式 ID 必須與物理目錄名（`soul/`）強一致。
- **糾偏**：當發生 ID 不一致（如 UI 仍使用舊版前綴）時，由 `AgentService.identify_agent` 進行單向映射糾偏，並在 API 回傳時強制轉換為正式 ID。
- **症狀**：側邊欄顯示 Agent 但點擊無反應，或是出現重複的 Agent。
- **恢復**：手動檢查 `config/models.json`，將帶有前綴的 `id` 改為簡潔版（如 `ToukaSoulAlice` -> `Alice`）。

### 2. 重置為預設配置 (Factory Reset)
- **需求**：設定檔損壞或想重新開始。
- **操作**：
    1. 備援並刪除 `config/` 下目標的 `.json` 檔案。
    2. 重啟 `ToukaProxy`。
    3. 系統將自動從 `.example` 重新生成純淨的初始設定。

---

## 🗺️ 物理資料存儲對照表 (Physical Layer)

| 功能模組 | SSOT 物理位址 (容器內) | 資料型態 | 權威說明 |
| :--- | :--- | :--- | :--- |
| **全域配置** | `/app/.env` | `${PROJECT_ROOT}/.env` | 最高的環境變數優先級。實際讀寫路徑由 `src.config.resolve_env_file_path()` 解析（見 Env SSOT），可經 **`TOUKA_ENV_FILE`** 覆寫（Docker 下＝`/app/host_root/.env`，ADR-0151 錨 compose 目錄／與 WORKSPACE_ROOT 解耦）。 |
| **自定義模型 (L2)** | `/app/data/custom/{id}` | `${SANCTUARY_DATA_PATH}/custom/{id}` | 包含 `config.json` 與 `system_prompt.md`。ID 為名稱轉換後的 Slug。 |
| **靈魂定義 (L1)** | `/app/data/soul/{id}` | `${SANCTUARY_DATA_PATH}/soul/{id}` | 包含 `soul.md` 與性格設定 |
| **原生模型 (L0)** | `/app/config/models.json` | `${PROJECT_ROOT}/ToukaProxy/config/models.json` | 靜態註冊的原生 LLM 清單 |
| **對話會話** | `/app/data/ui_sessions.db` | `${SANCTUARY_DATA_PATH}/ui_sessions.db" | SQLite 資料庫 |
| **統計與日誌** | `/app/data/stats/{id}/` | `${SANCTUARY_DATA_PATH}/stats/{id}/` | **分級統計 SSOT**: 依 Agent ID 強制隔離。 |
| **系統日誌** | `/app/data/logs/{id}/` | `${SANCTUARY_DATA_PATH}/logs/{id}/` | **分級日誌 SSOT**: 依 Agent ID 強制隔離。 |
| **舊對話存儲庫**| `/app/data/legacy/{id}/legacy_history.db` | `${SANCTUARY_DATA_PATH}/legacy/{id}/legacy_history.db` | **Legacy SSOT**: 搬遷後舊對話高效檢索的神經中樞。訊息 `timestamp` 一律毫秒：寫入經 `memory_server.utils.legacy_time.to_epoch_ms`（秒、數字字串、帶時區 ISO 都換算），修正前寫入的秒列在查詢與排序時才換算、不改既有列（ADR-0258）。系統歸檔（滑動視窗、切分存入、手動同步）只經 `archive_payload` 直接寫這個庫，訊息只新增不覆寫；表 `processed_files` 記手動同步已處理的（檔名, 內容雜湊）（ADR-0264）。 |
| **使用者頭像 (Avatars)** | `/app/data/avatars/{user_id}.{ext}` | `${SANCTUARY_DATA_PATH}/avatars/{user_id}.{ext}` | **Avatar SSOT**（ADR-0106）：頭像上傳檔，單檔/人、由 `avatar_service` 經 `ResourceManager.get_avatars_dir()` 定址、magic-byte 驗證（jpeg/png/webp、禁 SVG）、`is_relative_to` 防穿越。**已納入 `manage_backup.py` SANCTUARY_FOLDERS**。`users.json.avatar_url` 存服務路徑 `/v1/users/{id}/avatar?v=`（破快取）或舊外部 URL。公開 `GET` 服務（`<img>` 顯示內容）。 |
| **系統配置** | `${TOUKA_CONFIG_DIR}` | `${PROJECT_ROOT}/ToukaProxy/config` | **設定 SSOT**: 支援環境變數動態覆寫。 |
| **MCP OAuth 非秘密 Profile** | `${TOUKA_CONFIG_DIR}/mcp_oauth_profiles.json` | `JSON → OAuthProfile` | **OAuth Profile SSOT（ADR-0206）**：保存 immutable generation、callback binding、trusted public origin、CIMD/DCR issuer allowlist、exact private-network exception、scope policy 與 enabled；不含 credential。只有 verified human admin 可修改，per-profile AsyncFileLock＋SQLite updating fence 使 mutation 與 credential egress 互斥。 |
| **MCP OAuth Credential State** | `${TOUKA_CONFIG_DIR}/mcp_oauth.sqlite3`（含 WAL/SHM） | `SQLite + AES-256-GCM envelope` | **OAuth Runtime SSOT（ADR-0206）**：stable slot／active grant pointer、transaction HMAC lookup、encrypted grant/registration payload、CAS version、lease/fence、durable clock watermark 與 rotation phase。raw state 不落庫；`data/` 不參與此功能。一般備份固定排除 DB/WAL/SHM。 |
| **MCP OAuth Key Ring** | `.env: TOUKA_MCP_OAUTH_KEY*` | `Sensitive Environment` | **OAuth Key SSOT（ADR-0206）**：專用 32-byte current／retiring key、source、epoch 與 rotation phase；不得重用 JWT/Dispatch/Heartbeat key。managed mode 以 atomic secret writer 轉動，external mode 不由 API 產生 key material；備份與 API 永不輸出 key。 |
| **MCP Workload OAuth 非秘密 Profile** | `${TOUKA_CONFIG_DIR}/mcp_oauth_workload_profiles.json` | `JSON → WorkloadProfilePolicy` | **P8-B direct policy SSOT（ADR-0207）**：profile/generation、issuer/resource/token method、exact private-network policy 與 scope；不含 client ID/secret。admin mutation 以 generation CAS＋AsyncFileLock，JSON/SQLite transition 未收斂時資料面 fail-closed。 |
| **MCP Workload Owner Delegation** | `${TOUKA_CONFIG_DIR}/mcp_oauth_workload_delegations.json` | `JSON → WorkloadDelegationPolicy` | **P8-B delegated authority SSOT（ADR-0207）**：owner＋execution agent＋installation/generation＋profile/generation＋P8-A grant ID/version＋raw tool＋scope＋expiry。source grant fields 只由 server 推導；SQLite 保存 exact digest mirror，不保存 bearer。 |
| **MCP Workload Credential／Runtime** | `${TOUKA_CONFIG_DIR}/mcp_oauth_workload.sqlite3`（含 WAL/SHM） | `SQLite + AES-256-GCM envelope` | **P8-B secret/runtime SSOT（ADR-0207）**：client ID/secret、metadata snapshot、delegation mirror、authority epoch、exact active mutation operation ID/kind、revocation/rotation/recovery FSM 與 durable audit outbox。revocation row 保存 source-slot digest＋mutation operation ID；`runtime_authority_ready()` 同時納入 transition/revocation/rotation/recovery-required/durable owner blocker。audit sink 暫時失敗時 pending row 不 ack，maintenance 以固定間隔重試且 cancellation 可終止。舊 row 無可證 owner 時只遷移為 `recovery_required`。只在 single Proxy process＋local filesystem lock 下啟用；一般備份固定排除 DB/WAL/SHM。 |
| **MCP Workload Key Ring** | `.env: TOUKA_MCP_OAUTH_WORKLOAD_KEY*` | `Sensitive Environment` | **P8-B key SSOT（ADR-0207）**：獨立 current/retiring/source/phase/epoch，不得重用 P8-A key；managed rotation 先 drain boot-local egress permit再 retire，external mode不由 API 產生 key。備份/API 永不輸出。 |
| **MCP Workload Topology／Permit** | `${TOUKA_CONFIG_DIR}/mcp_oauth_p8b_runtime.lock`＋Proxy process memory | `OS exclusive lock + boot-local immutable permit` | **單一 process 正確性 SSOT（ADR-0207）**：filesystem kind 必須明示 local；第二 process/remote/unknown 拒絕啟動。bootstrap 的單一 `WorkloadAuthorityMutationCoordinator` 由 control/rotation 共用，擁有 mutation lock 與 exact global-fence token；fence complete 同時核對 durable operation owner，restart 只能從 store 的 exact owner 重建。permit admission 同時重驗 durable readiness。OAuth credential attempt 綁 final frozen `POST` request 的 secret-free digest，delegated refresh 的 permit/send 必須消費同一 prepared request；tool-ticket replay index固定容量並按 expiry prune。TTL 只限制 begin-send，完成需 task exit/cancel ack。 |
| **本機 runtime／GHCR 發行** | `docs/architecture/adr/0220-local-only-github-ghcr-release-profile.md`＋`docs/architecture/adr/0238-retire-release-qualification-machinery.md`＋`.github/workflows/ghcr-publish.yml` | Owner-selected policy＋最小 publish workflow | **本機邊界與最小發行 SSOT（ADR-0220／0238）**：Touka 產品的**本機 runtime／data**不上雲；GitHub 只承載原始碼與 CI，GHCR 只承載 `touka-proxy`／`touka-heartbeat`／`touka-chat` 三映像。只接受手動觸發：一律發布 `sha-<commit>`，填入 `version` 時另發布該版號＋`latest`（不用 tag 觸發，避免與 ToukaIDE Marketplace 的 `v*` tag 衝突）；不再有 qualification、seal、receipt 或 readback 程序。 |
| **Modern-only MCP Policy** | `docs/architecture/adr/0221-modern-only-local-cold-cutover.md` | Owner-selected retirement policy | **本機 legacy 退場 SSOT（ADR-0221 modern-only 部分；其發行／冷切換程序已由 ADR-0238 取代）**：所有 client 只在同一本機、沒有舊版使用需求；保留 modern request-scoped SSE，舊 HTTP+SSE 入口回 404。不安排 legacy 調查／遷移。 |
| **ToukaIDE Deterministic VSIX** | `ToukaIDE/package.json`＋`ToukaIDE/scripts/deterministic_vsix.py` | package projection＋offline normalizer／verifier | **Universal VSIX byte identity SSOT**：extension identity與版本由 `deterministic_vsix.py` 固定（現行`touka-ai.toukaide@0.3.0`）、MIT；repo-root hash-bound`LICENSE`只注入repo外staging，不新增tracked`ToukaIDE/LICENSE`。Verifier要求 `EXPECTED_MEMBERS` 全等、無native payload／unsafe path／duplicate，normalizer固定order／timestamp／mode／stored ZIP並create-only。這只證明offline deterministic boundary，不代表`vsce package`、Marketplace publish或readback已完成。 |
| **ToukaIDE Marketplace Candidate** | `ToukaIDE/package.json`＋`package-lock.json`＋`scripts/build_marketplace_candidate.py`＋`scripts/deterministic_vsix.py` | `touka.ide.marketplace-candidate.v1` canonical manifest＋一份universal VSIX | **現行IDE發行SSOT（ADR-0218）**：`@vscode/vsce@3.9.2` direct exact pin；以 exact Node 直接執行 package-local npm 與 repo-local vsce。Builder要求clean release inputs與版本tag→HEAD，bundle後重驗source，repo外create-only輸出並綁commit／tag／publisher／MIT／archive與exact members。Marketplace登入／upload／readback仍是獨立人工gate；Open VSX不在範圍。步驟見`docs/guides/toukaide-marketplace-release.md`。 |
| **Dependency Audit Coverage／CPU Source Identity** | `scripts/dependency_audit_coverage.py`＋`docs/security/torch-cpu-advisory-identity.json` | frozen lock projection＋exact query pairing＋官方來源紀錄 | **ADR-0223 coverage/error-path 實作切片已接受**：覆蓋跨平台 marker，CPU advisory 身分綁官方來源與 hash；first-party editable 由 workspace 身分獨立分類。缺列、錯配、未證明來源／skip 均 INCOMPLETE；JSON 不是版本 lock 或弱點豁免清單。 |
| **SNS 表單憑證與退役 Storage Keys** | SNS 單一領域 Hook＋`ToukaChat/src/hooks/useLocalStorage.ts` 清除接縫 | React memory-only state＋精確 retired-key 清單 | **ADR-0224 SNS 修復切片與完整前端驗證已接受**：全 app 接線清除舊 SNS keys，不讀回秘密、不清無關 storage；單 Hook 擁有表單，成功／失敗／卸載 scrub。尚未部署，關閉／離線舊 client 必須載入新版才能清除；不宣稱 secure erase 或已全數清空。 |
| **自癒範本** | `ToukaProxy/config/*.json.example` → `${TOUKA_CONFIG_DIR}/*.json` | `Git Tracked → Runtime` | **SSOT 恢復基準（ADR-0209）**：來源範本唯讀且受 Git 追蹤；只在目標缺少時建立 runtime JSON，不覆寫既有設定。Production 來源／目標可同目錄；pytest 目標只能是 session temp sandbox。 |
| **認證密鑰 (Secret)**| `.env: TOUKA_AUTH_SECRET` | `Sensitive Environment` | **認證 SSOT**: 簽署與驗證 JWT Token 的唯一事實來源。公開值拒用見 ADR-0251（`config.PUBLIC_SECRET_VALUES`）。 |
| **身分與能力 (Ident)** | `${TOUKA_CONFIG_DIR}/identity_map.json` | `JSON` | 身分標籤規則、個體角色 Override (role)、**心跳喚醒權限 (capabilities: heartbeat_awakening)** 與 **工作分派特權代理 (capabilities: can_supervise_dispatch)** 的權威來源。 |
| **角色權限 (Permissions)** | `${TOUKA_CONFIG_DIR}/permissions.json` | `JSON → PermissionsConfig` | **權限 wire SSOT（ADR-0196）**：角色工具／技能／MCP allow 與 self-only deny、全域 deny 地板。執行期有效答案唯一由 `PermissionService.resolve_grants()` 解析為不可變 `RoleGrants`；bootstrap fallback 原始資料在 `src/utils/permission_defaults.py`，只保留 Owner 修復通道。生產／`.example`／fallback 同受 Guard #32 管制；啟動壞檔 fail-fast，熱重載壞檔保留 last-known-good＋degraded health。 |
| **群聊白名單 (Alias)**| `${TOUKA_CONFIG_DIR}/group_whitelist.json` | `JSON` | **群聊 SSOT**: 定義 `@別名` (Key) 與 **靈魂 ID** (Value) 的映射關係；只認 `ToukaSoul` 區，舊 `Native`／`Custom` 區讀到就略過並記警告、不改檔（群聊只限靈魂，ADR-0245）。 |
| **安全策略 (Safety)** | `/app/config/auth_policies.json` | `JSON` | 核心 Regex 黑名單與工具風險分類（`tool_risk`）；三種模式的決策表在 `models/safety.py`，底線是程式常數（`utils/hard_floor.py`），都不在此檔、設定檔關不掉（ADR-0246）。 |
| **測試基礎設施 (Tests)** | `/tests/` | `Python (Pytest)` | **測試大一統 SSOT**。全系統的所有邏輯、安全性與整合驗證一律在此通過；pytest／Vitest／自動 smoke 均不得依賴實體資料寫入或計量 API。 |
| **Smoke 執行分類／Live API 人工閘** | `tests/smoke/manifest.py` ＋ `tests/live_api_policy.py` | immutable Python registry | **ADR-0214 執行分類 SSOT**：`manifest.py` 使每個 smoke path 恰好分類一次；`AUTOMATED_SANDBOXED` 只含逐程序 temp write-root sandbox、provider mock／credential-shaped env scrub 與零計量 API 項目，manual／metered 項目永久排除 automation並顯示 `NOT RUN — HUMAN APPROVAL REQUIRED`。`live_api_policy.py` 提供人工 operation 分類、CLI preflight、exact scope capability 與 request budget；改造後的 CLI 每次須先取得人類有界同意再帶 `--human-approved-live-api`，但旗標不是身分認證或不可偽造的人類意志證明。Debug Center endpoint 目前只列分類，後端尚未消費 capability。Credential scrub／offline flags 不等於 OS egress jail。 |
| **自主任務看板** | `/app/data/personal/{id}/missions/` | `JSON (Pydantic)` | **Mission Board SSOT**: 儲存 Agent 目前進行中的任務樹與進度。`DispatchAgent` 的看板路徑為 `/app/data/personal/DispatchAgent/missions/current_mission.json`，支援具備 `can_supervise_dispatch` 權限的跨身分代理讀寫。任務狀態更新強制執行 FSM 狀態轉換矩陣與 `FAILED` 狀態特權重置限制，且產出物屬性解耦為 `artifact_ref` (str) 與 `artifact_refs` (list[str]) 以防覆蓋踩踏。 |
| **API 合約 (OpenAPI)**| `openapi.json` | `JSON (Git Tracked)` | **Contract SSOT**: 後端 Pydantic 模型的物理導出基準。由 `test_api_schema_sync.py` (後端) 與 `guard_frontend.ts` (前端) 強制校驗同步性。 |
| **工具定義 (Schema)** | `schemas.py` | `${TOUKA_MEMORY}/tools/schemas.py` | **Tool SSOT**: 所有記憶與檔案庫工具的參數定義權威來源。 |
| **工具執行中繼資料** | `ToukaProxy/src/tools/execution_context.py` | `ToolExecutionContextKwargs`＋`EXECUTION_CONTEXT_KWARG_NAMES`＋builder | **Execution metadata SSOT（ADR-0214）**：ToolExecutor 注入的 request／session／model／HITL／event／context kwargs 唯一清單；領域身分 `identity` 不屬於此集合。Memory gateway 必須依此集合剝除中繼資料與 `resource_proxy`，但保留 identity 與記憶領域參數。 |
| **自定義連線** | `connections.json` | `${TOUKA_CONFIG_DIR}/connections.json` | **Connection SSOT**: 自定義 OpenAI 相容模型來源的權威定義。 |
| **模型發現快取** | `models.json` | `${TOUKA_CONFIG_DIR}/models.json` | **Model Cache SSOT**: 經發現與健康檢查後的模型狀態持久化層。 |
| **錯誤碼定義** | `errors.json` | `${TOUKA_CONFIG_DIR}/errors.json` | **Error SSOT**: 跨端唯一錯誤代碼、狀態碼與 i18n 映射來源。由 `sync_errors.py` 同步至前端。 |
| **翻譯目錄 (i18n)** | `locales/<lng>/translation.json` ＋ `locales/index.ts`（語言註冊表） | `ToukaChat/src/locales/` | **Translation Catalog SSOT**（ADR-0093）：react-i18next 翻譯目錄，一語言一檔；`zh-TW` 為來源語言、`en` 為國際參考、其餘社群語言。`index.ts` barrel export `SOURCE_LNG`/`SUPPORTED_LNGS`/`i18nResources`（新增語言＝放檔＋加一行）。由 **Guard #33**（`guard_i18n.ts`）以 zh-TW 為基準驗證跨語言 key 同步（多餘/拼錯＝錯誤、`en` 不完整＝錯誤、社群語言缺 key＝警告、複數 `*_one/_other` 允許）。錯誤訊息 `error.*` 橋接 `errors.json` 的 `i18n` key（ADR-0014），由 `errorMsg()` 在地化。`fallbackLng='en'`；語言記憶由 `i18next-browser-languagedetector` 寫 localStorage `touka_lang`。⚠️ 勿開 `nonExplicitSupportedLngs`（會壞 zh-TW 解析）。 |
| **滑動視窗設定** | `sliding_window.json` | `${TOUKA_CONFIG_DIR}/sliding_window.json` | **SlidingWindow SSOT**（ADR-0035 amended by ADR-0092）：家對話滑動視窗的啟用狀態與 **token 百分比規則**（`trigger_pct` 觸發／`target_floor_pct` 地板／`summary_budget_pct` 摘要上限／`min_keep_pairs`／`summary_model`）。由 `SlidingWindowService.load_config()` 讀取，`POST /v1/system/sliding_window` 更新（admin only）。觸發點兩處（heartbeat finally ＋ dispatcher turn 落盤後、經 per-session 鎖；禁令 28/57）。舊 `keep_rounds`/`upper_limit_rounds` 為 deprecated 相容欄位（則數計算 SSOT `count_rounds_from_linear` 保留供 session 列表/ContextMeter 顯示）。 |
| **Context Token 量測** | `context_window_registry._BUILTIN_WINDOWS` ＋ `config/model_context_windows.json` | code 表＋JSON 覆蓋 | **Context Window SSOT**（ADR-0092）：模型 context window（token）分母。`get_context_window(model)` 內建表為主、JSON `windows`/`_default` 覆蓋、未知回 128K。`token_estimator.estimate_tokens/estimate_messages_tokens`（CJK 感知、fail-safe、永不拋例外）為 token 估算唯一入口；`context_breakdown.compute_context_breakdown` 以 authoritative `input_tokens` 校正分類別用量（供 ContextMeter）。 |
| **Prompt 區塊邊界** | `prompt_blocks.py`（tag 常數＋`wrap_block`） | `ToukaProxy/src/services/prompt_blocks.py` | **Prompt Delimiter SSOT**（ADR-0092 B0）：各 context 區塊以一致 XML tag 包覆（`<persona>`/`<project_context>`/`<ide_context>`/`<mission_awareness>`/`<capabilities>`/`<conversation_summary>`），供 breakdown 單一 parser 切段；`ALL_BLOCK_TAGS` 為列舉來源。soul 外層 `<persona>` 內文逐字不變（不碰 `data/soul`）。 |
| **對話摘要欄位** | `user_sessions.context_summary` | `ui_sessions.db` | **Context Summary SSOT**（ADR-0092 D）：session 級「被剪舊對話的摘要」（替換式、不進對話訊息樹故不被再剪/再算/再摘要）。`sliding_window` 剪裁時經 `dialogue_summarizer`（haiku、fail-safe）寫入；`SoulLayer.inject_context_summary` 前置注入為 `<conversation_summary>`（無痕不注入）。文字第一行是系統寫的時間範圍（ADR-0264）。 |
| **依賴主權 (Deps)** | root＋四member `pyproject.toml` → 單一`uv.lock` | uv workspace | **Dependency SSOT（ADR-0117／0222）**：人只改root或member的抽象dependency declaration（`~=`／`==`，禁`>=`），`uv.lock`是生成且不可手改的全workspace唯一lock；現行members為Proxy、Memory、Heartbeat、Dispatch，torch CPU來源亦鎖在其中。佈建一律`uv sync --all-packages`，裸`uv sync`會把虛擬root同步成空環境。Guard #16驗current-lock sandbox import與Docker parity，沙盒快取在git common dir（`.git/touka-guard-cache/<platform>/`，所有worktree共用、成員鎖序列化，鍵含uv sync配方與`.python-version`；ADR-0240）；#30離線驗declaration↔lock與Python版本。Guard #22固定把frozen lock匯出成repo外`runtime --no-dev`與`build-test --all-groups`兩scope，再以locked pip-audit fail-closed判讀；缺tool不安裝、空白／非JSON／錯誤exit與無權威severity不得成為clean。 |
| **Frontend 供應鏈** | `ToukaChat/package.json`＋`package-lock.json`＋`.npmrc`；`ToukaIDE/package.json`＋`package-lock.json` | npm policy／resolution／lifecycle／audit | **ADR-0174／0222**：Chat build toolchain以engines／packageManager／devEngines精確鎖定Node.js／npm（版本以`package.json`為準）；`next`與`eslint-config-next`的direct spec均exact，lock為解析SSOT，專案不維持`sharp` override。IDE以packageManager／devEngines鎖同一build toolchain，但extension runtime/API floor保持Node 20／VS Code 1.90。lock package只允許official registry＋SHA-512 integrity；install永遠`ignore-scripts`／`no-audit`／`no-fund`。`scripts/hermetic_gate/npm_offline_gate.py`是六鍵純policy/parser與offline cache-only lifecycle SSOT（Guard23共用）；bundle allowance僅替補有official／SHA512父tarball明示宣告的缺失child欄位，所有明示URL/integrity先驗；固定NEXT_TELEMETRY_DISABLED=1。`scripts/guard_all.py`在local guards前驗exact Chat runtime與Node user-profile capability。Guard #23由`guard_node_cve.py`直接以絕對Node＋npm-cli argv掃Chat／IDE各runtime與full graph，使用repo外最小無憑證環境與public registry，parser／exit不一致fail-closed，且永不fix／install／retry。Docker build停用Next telemetry，production Chat只用本地`serve`且停用update check。 |
| **Production image provenance** | 三產品 Dockerfile（`ToukaProxy/Dockerfile`、`ToukaHeartbeat/Dockerfile`、`ToukaChat/Dockerfile.prod`） | digest 釘版官方 base＋Debian 安全更新 | **ADR-0238**：Python 基底為 digest 釘版的官方 `python:3.12-slim-trixie`；Node／npm 直接由 digest 釘版官方 node 映像複製；Debian 套件以 `apt-get upgrade` 取官方安全更新。Chat 保留 braces 同版本防禦回補（ADR-0237），builder 驗 full scope、runner 驗 runtime scope。不再自編 Node、自組 npm、重編 Debian 套件或做映像 qualification；一般 `docker compose build` 即可建置。三個映像的 `/app` 都帶 repo 根的 LICENSE、NOTICE：Chat 的 build context 只有 `ToukaChat/`，授權檔取自具名 context `touka-root`（compose 的 `additional_contexts`、GHCR 發布的 `build-contexts` 已設好；手動 `docker build` 要加 `--build-context touka-root=.`，沒加時建置失敗）；Chat runner 以 `--omit=optional` 安裝，不帶進 sharp 的 LGPL libvips（`tests/lint/test_release_licensing.py`）。 |
| **Embedding 模型 (ML)** | `.env: EMBEDDING_MODEL` | 環境變數覆蓋 | **Embedding SSOT**（ADR-0053、ADR-0070；download/switch 生命週期 ADR-0118）：ToukaMemory 語意向量化的 SentenceTransformer 模型。**目前為 `intfloat/multilingual-e5-base`（768 維、多語、MIT）**；e5 系列需 `query:`/`passage:` 非對稱前綴，由 `vector._format_for_embedding()` 依模型名自動處理（非 e5 不加、向後相容）。查重門檻與查詢下限用同一個家族判斷（`vector.embedding_family`）查表（ADR-0265；e5 0.93／0.65，其他 0.92／0.3）；換成 e5 以外的模型或 e5 其他尺寸時重跑人工 probe `tests/smoke/manual/embedding_threshold_probe.py`。`EMBEDDING_MODEL` 要寫完整的 `intfloat/multilingual-e5-base`：少了組織時 sentence-transformers 會補成不存在的 `sentence-transformers/multilingual-e5-base` 而載入失敗。程式內建預設仍為 `all-MiniLM-L6-v2`。由 `config_utils.get_embedding_model_name()` 讀取，`VectorStore`/`MemoryManager` 使用。**換模型若維度改變，須以 `ToukaMemory/scripts/reembed_memory.py` 從 FactualStore 重建向量（建新+置換，因 Qdrant local 無法原地改維度）。**<br>**下載／切換生命週期 SSOT（`ToukaProxy/src/services/embedding_service.py`）⭐ — ADR-0118（2026-07-02）**：三動作嚴格分離——**(1) DOWNLOAD**＝抓模型檔至 HF cache（背景任務＋輪詢狀態、逐檔 `hf_hub_download` byte 進度、`snapshot_download local_files_only` 快取跳過；**純加法**，永不 `force_download`、不刪任何模型，不碰向量/事實）；**(2) SWITCH**＝寫 `EMBEDDING_MODEL` ＋ 重置 `MemoryManager` 單例（免重啟），`evaluate_switch` 偵測既有記憶（`FactualStore.query_all_facts`），當有記憶且模型不同時**強制使用者確認（`force` 旗標）**；**(3) RE-EMBED**＝離線維運限定（`reembed_memory.py`），**永不自動觸發**、只讀事實、不升級記憶庫結構（版本落後就拒絕執行；事實為無損 SSOT、向量為衍生索引）。端點：`POST /v1/admin/setup/embedding/download`、`GET /v1/admin/setup/embedding/download-status`、`POST /v1/admin/setup/embedding/switch {model, force}`。 |
| **HF 模型快取 (HF Cache)** | `/app/hf_cache`（`.env: HF_HOME`） | Docker named volume `hf-cache` | **HF Cache SSOT**（ADR-0118 extends ADR-0038，2026-07-02）：HuggingFace hub 快取根，**單一變數 `HF_HOME` 同時涵蓋**首裝精靈的 embedding 下載與 `vector.py` runtime `SentenceTransformer` 載入（兩者共用同一 hub cache）。`docker-compose.yml` 新增頂層 named volume `hf-cache` 掛於 touka-proxy 的 `/app/hf_cache`；`touka-init` 掛同一 volume 並 `chown /app/hf_cache 1001:1001`（新建 named volume 為 root 所有，appuser 需先 chown 方可寫，**強制**）。`docker compose up --build` 保留已下載模型；`docker compose down -v` 清空。無 Dockerfile 變更。 |
| **向量記憶雙庫** | `vault/core_memory.db` ＋ `vault/_qdrant_db/` | 各 agent 資料目錄 | **Memory SSOT**（ADR-0070）：記憶**雙寫**——`FactualStore`（`core_memory.db`，**無損事實**、SQLite、content 逐字、權威來源）＋ `VectorStore`（Qdrant，語義**索引**）。事實以 FactualStore 為準、可由其重建向量；查詢雙軌（SQL 精確 ＋ 向量語義）。索引可丟可重建，事實不可。結構版本由 `factual.FACTS_SCHEMA_STEPS` ↔ `PRAGMA user_version` 管；只在 Proxy 啟動時經 `FactualStore.shared()` 升級，離線工具 `allow_schema_upgrade=False`、版本落後就拒絕；升級前備份到 vault 裡、`core_memory.db` 旁的 `FACTS_BACKUP_DIR_NAME` 資料夾，份數 `FACTS_BACKUP_KEEP`；失敗狀態 `FactsSchemaStatus` 經 `/v1/memory` 的 `memory_notices` 顯示。向量寫不進去不刪事實：標 `EmbeddingState.FAILED`，語意結果一律回查事實庫，`memory_tools(action="reindex")` 手動補（`services/index_state.py`）（ADR-0256）。 |
| **人類使用者 (Users)** | `users.json` | `${TOUKA_CONFIG_DIR}/users.json` | **User SSOT**: 全系統人類使用者帳號、密碼 Hash、Refresh Token 效期、`refresh_token_jti`（jti replay 防護）、帳號 `status` 與 **Agent 擁有權 (owned_agents)** 的唯一事實來源。**帳號是否可用**由衍生 property `User.is_active`（`status=="active"`，DA23）判定，於登入/`verify_token`/`get_current_user`/refresh 四閘門強制。 |
| **專案歸屬 (Projects)** | `projects.owner_user_id`（ui_sessions.db） | `${SANCTUARY_DATA_PATH}/ui_sessions.db` | **Project Ownership SSOT**（ADR-0107）：`projects` 表 `owner_user_id` 欄位＝專案明確人類擁有者（`save_project` 於建立時由人類 JWT sub 設定）。存取控制「明確 owner 優先、無 owner（legacy）退回 session agent 反推」。自助清空（`POST /v1/auth/clear-my-data`）刪 owner 相符的設定列、sessions `project_id` 置 NULL（保留對話）、**不刪 `root_path` 實體資料夾**。 |
| **會話歸屬 (Sessions)** | `user_sessions.owner_user_id`（ui_sessions.db） | `${SANCTUARY_DATA_PATH}/ui_sessions.db` | **Session Ownership SSOT**（ADR-0132，ADR-0107 同構）：session 明確人類擁有者——human 建立時記錄（`save_session` server-side attribution、client 不可指定）、**僅 INSERT 寫入、UPDATE 永不改**。授權為**聯集通道**（admin/Debugger → owner 本人 → 既有 agent-derived `is_owner`，零縮窄）；NULL＝legacy 行為不變。出口（list/get）一律 pop（internal authz field）。`upsert_session` 的 `identity` 參數語意固定為「session 所屬 agent」（human 呼叫者不再污染 `agent_id`）。legacy NULL rows 由 `scripts/backfill_session_owner.py`（dry-run 預設、只填 NULL、冪等）手動歸屬。 |
| **首裝 admin 密碼 (DA21)** | `INITIAL_ADMIN_PASSWORD.txt` | `${TOUKA_CONFIG_DIR}/INITIAL_ADMIN_PASSWORD.txt` | **Bootstrap PW SSOT**（路徑＝`config.get_initial_admin_password_file()`）：首次無 admin 時寫入隨機初始密碼的**一次性檔**，供操作者取回，**取代**原本明文寫持久日誌的作法（日誌僅記路徑）。首次改密或啟動時（admin 非臨時密碼）由 `resource_manager.remove_file` 自動清除。 |
| **Session TTL 設定（已退役）** | — | — | **已撤回（ADR-0259）**：P28 的 `SESSION_TTL_DAYS` 與 `cleanup_expired_sessions()` 已刪除，對話不再自動刪除。`user_sessions.last_activity_at` 欄位保留，只記最後一次寫入時間，不再驅動任何刪除。 |
| **請求體大小限制** | `config.py: MAX_REQUEST_BODY_BYTES` | 環境變數覆蓋 | **Request Size SSOT**（P19）：`RequestSizeLimitMiddleware` 讀取此值；預設 10MB；超限回 413 `REQUEST_TOO_LARGE`。 |
| **技能目錄 (Skills)** | `${TOUKA_CONFIG_DIR}/skills/<skill-id>/` | `目錄 (SKILL.md)` | **Skills SSOT**（ADR-0066）：每個技能為一目錄，含 Anthropic 標準 `SKILL.md`（YAML frontmatter，`name`+`description` 必填；Touka 擴充欄位 `display-name`/`category`/`author`/`is_system` 收於 `metadata:` 子欄位以通過標準 `quick_validate`）+ 選填 `.mcp.json`（MCP 連線）/`scripts/`/`references/`/`assets/`。`mcp_servers` 改由同目錄 `.mcp.json` 的 top-level keys 推導（不再是 frontmatter 欄位）。由 `SkillService` 以三層 Progressive Disclosure 載入（metadata 常駐 → body 觸發 → resources 按需），`GET /v1/skills/list` 回傳。`.skill`（ZIP）可經 `GET /v1/skills/{id}/export` 打包、`POST /v1/skills/install` 由 URL 安裝。`is_system: true` 的技能受保護不可刪除。 || **Skill Catalog 安裝 (Real-Skill Catalog)** | `config/skill_catalog.json.example` | tracked 種子（JSON） | **Skill Catalog SSOT**（ADR-0074）：真技能（能力型 `.skill`，含 `scripts/`）的安裝策展來源。每筆 `{id,name,category,description,repo,commit(40-hex),base_path,files[],author,license}`；`install_skill_from_catalog` **只認 catalog 內已審 id**（使用者不自填 URL），逐檔抓 `raw.githubusercontent.com/<repo>/<commit>/...`（**runtime 零 `api.github.com`**），重用既有真安裝防護（路徑穿越／大小上限／`.bak` 原子上線）。bootstrap 自癒植入、`get_catalog()` fallback。只收 `{Apache-2.0, MIT, BSD-3-Clause}`（避開 source-available 文件類；entry files 含 LICENSE）；含可執行腳本須 UI 警示＋二次確認＋Owner/admin。策展工具 `scripts/curate_skill_catalog.py` 屬 dev-time（用 api.github.com 列檔），嚴禁進 runtime `src/`。**版本追蹤**：安裝在技能目錄寫 `.touka_source.json`（`{catalog_id,commit,repo,installed_at}`）為**安裝來源 SSOT**（不改寫上游 SKILL.md）；`list_skills` 暴露 `source_commit`，前端純函式 `computeCatalogStatus` 衍生「最新／可更新」，升級＝重裝（覆寫並更新 sidecar）。**內容完整性**：每檔釘 `sha256`（`CatalogFile{path,sha256}`，path 與 hash 同處一筆），安裝逐檔比對下載內容雜湊、不符即拒（commit pin 之上的防禦縱深）。 |
| **派發召喚白名單 (Dispatch Hooks)** | `ToukaDispatch/config/dispatch_hooks.json` | `JSON` | **Auto-Invoke SSOT**（ADR-0067）：`on_mission_created.<executor>` 定義自動召喚外部 AI 的 `command`（白名單 list，`shell=False` 零注入）與 `pass_env`。`dispatch_initialize_mission(..., auto_invoke=<executor>)` 依此白名單，以 env var 傳遞一次性 `mission_token`（非全域 `DISPATCH_TOKEN`）召喚外部 AI。新增 executor 只改此檔（Configuration-as-Code），fail-closed（缺檔/壞檔/未列入 → 不召喚，mission 仍建立）。部署須限 Owner 寫。 |
| **創建編排 (Creation Engine)** | `ToukaProxy/src/services/creation/` | `CreationEngine` | **Creation SSOT**（ADR-0069）：MCP 與 Skill 創建的單一編排入口。`creation_engine.create(kind, spec)` 執行 VALIDATE → SMOKE → COMMIT →（後置）EVAL 流水線；`CreationKind` 策略（`skill`/`mcp`）分別委派 `SkillService.update_skill`（`config/skills/` SSOT）與 `MCPHubService.register_server`（`server_config.json` SSOT），不另立事實來源。CREATE 不覆寫既有 id（保護既有 agent 資料）。命令安全統一於 `src/utils/mcp_security.py`。SMOKE 試跑（淨環境啟動 MCP 握手 / skill 乾跑、逾時+保證拆除）與 EVAL（commit 後**非阻斷**生成唯讀評測基線 `config/evals/<kind>/<id>.eval.md`、LLM 計分預設關閉）已實作；該評測基線為**衍生回歸物、非新 SSOT**。SCAFFOLD（Phase C）：`McpScaffolder`（`creation/scaffold.py`）決定性 stub 生成 FastMCP server 至 `config/mcp_servers/<name>/server.py`（衍生產物、`command=sys.executable`），餵入既有 create 流水線；注入防護以 `repr()` 轉義＋識別字校驗＋`ast.parse` 兜底＋路徑 slug 防穿越；不覆寫既有產物；名稱正規化統一於 `creation/naming.py`。Phase D（對外開放）：`/v1/creation` 路由（`routers/creation.py`，Owner/admin）＋ agent 工具（`tools/builtin/creation.py`）共用薄門面 `services/creation/api.py`（DRY/SSOT）；`ModelJudge`（`model_judge.py`）把 Phase B 的 Judge 接縫接上 LLM（雙閘預設關、零成本）；Creation Sovereignty Guard #29（`scripts/guard_creation_sovereignty.py`）強制路由 Owner/admin 把關＋禁繞引擎直寫。 |
| **架構守衛編號 (Guards)** | `scripts/guard_all.py` | `scripts/guard_all.py` | **守衛編號與順序 SSOT**：以 `guard_all.py` 任務陣列為 43 道關卡（#1～#41、#44、#45）的唯一事實來源（#28 ADR 完整性、#29 創建主權、#30 環境一致性、#31 孤兒測試、#44 Ruff Banned-API、#45 原生工具契約）。#42 Release Workflow Gate 與 #43 ghcr Compose Pull-Purity 已由 ADR-0238 退役，編號不重用。所有文件編號與控制台輸出必須與其對齊。 |
| **測試收錄 (Test Include / Orphan Guard)** | `ToukaChat/scripts/lib/orphaned_tests_logic.ts: TEST_INCLUDE_GLOBS` | TS 常數陣列 | **測試收錄 SSOT**：vitest `test.include` 與「孤兒測試守衛」（Guard #31，`scripts/guard_orphaned_tests.ts`）共用同一份 glob（`src/`、`tests/`、`scripts/` 下 `*.{test,spec}.{ts,tsx}`），防止測試檔落在收錄範圍外而**靜默不執行**。配套：`guard:time`（#3）擴充禁用 Date 顯示方法 `.toLocaleString/.toDateString/.toTimeString`，強制日期顯示走 `formatToTaipeiTime`（時區一致），數字格式化改用 `Intl.NumberFormat`。 |
| **MCP Hub 分類 (Hub Category)** | `ToukaProxy/src/utils/mcp_category.py: MCP_HUB_CATEGORIES` | 模組常數（tuple） | **MCP Hub 分類 taxonomy SSOT**：hub server 功能性分類（`Browser`/`Dev`/`File`/`Database`/`Utils`）的唯一來源。純函式 `classify_mcp_category(name, description, npm_package)`（word-boundary 關鍵字、catch-all `Utils`，恆回傳合法分類）於 `MCPHubService` 同步時標記 `category`，**取代**舊的 `Official`/`Community`（官方/社群身分改由獨立的 `source`/`is_official` 欄位保留、不流失訊號）。`MCPHubServerItem.category` 經 OpenAPI 契約（`update_openapi.py`→`sync-api`）流到前端；`McpHubView` 由 hub 資料**衍生**分類頁籤（不硬編 `["All",...]`）→ 從根本杜絕前後端分類漂移與「除『全部』外永遠空白」的頁籤 bug。 |
| **文件 MCP 整併 (mcp-docgen)** | `config/mcp_hub.json`(.example) curated 條目 ＋ `config/skills/document-generator/` | tracked 種子＋技能目錄 | **mcp-docgen 整併 SSOT**（ADR-0079）：第一方開源文件 MCP（PyPI `mcp-docgen`，MIT，clean-room）以 curated 條目（`id=curated-mcp-docgen`、`source=curated`、`is_official=false` 誠實標示非官方、`category=File`、recipe `uvx mcp-docgen`、`env.MCP_DOCGEN_OUTPUT_DIR=@TOUKA_OUTPUT@`）收錄 MCP Hub。`MCPHubService.sync()` 保留 `source=="curated"`（外部清單不覆寫第一方）；`install()` 把哨兵 `@TOUKA_OUTPUT@` 解析為 `resource_manager.get_temp_path("docgen_output")` 共用輸出夾。薄技能 `document-generator`（`.mcp.json` 宣告 `mcp-docgen`）教 agent 三工具與 Markdown 慣例；bootstrap ESSENTIAL_CONFIGS 含 `mcp_hub.json`（缺檔自癒）。per-agent 輸出隔離列未來增強。 |

---

## 🧠 邏輯事實與準則 (Logical Layer)

### 1. 時間 SSOT 原則
*   **基準來源**: 全系統時間獲取必須透過 `src.utils.time_utils` (後端) 或 `src/lib/time_utils.ts` (前端)。**由 `lint_ssot.py` (後端 AST) 與 `guard_time.ts` (前端) 進行靜態稽核與強制執行。**
*   **儲存與計算**: 系統內部一律使用 **UNIX Epoch Time (int/ms 或 float/s)** 作為唯一時間基準。
*   **呈現層轉換**: 僅在 UI、日誌輸出或 Prompt 注入時轉換為 **Asia/Taipei (UTC+8)**。使用 `formatToTaipeiDate` 系列函式確保顯示一致。
*   **給模型看的歷史時間**: 舊對話搜尋、隨機回想（舊對話與事實）、查記憶的日期一律經 `memory_server.utils.legacy_time.format_taipei`，台北時間並標 `Asia/Taipei`，與 `<continuity>` 同時區；`YYYY-MM-DD` 日期參數當成台北的一整天（`taipei_day_start`）。Memory 不能 import Proxy 的 `time_utils`，這是 Memory 端的純函式（不取現在時間；現在時間仍經 `config_utils.get_current_epoch`）（ADR-0258）。

### 2. 路徑安全主權 (Path Security SSOT) ⭐
*   **邊界檢查**：所有用戶可控的路徑輸入必須使用 `Path.is_relative_to(allowed_root)` 執行邊界檢查（禁止 `str.startswith()`）。
*   **Symlink 防護**：Vault 路徑存取前必須呼叫 `validate_no_symlink_escape(path, allowed_root)`（`ToukaProxy/src/utils/path_security.py`），透過 `Path.resolve()` 解析所有 symlink 後再行邊界檢查。
*   **雙重保護**：`is_relative_to()` + `validate_no_symlink_escape()` 組合確保路徑穿越與 symlink 逃逸均被封堵。

### 3. 身分物件化主權 (Identity SSOT) ⭐
*   **物件化依賴**: 系統 Service 層、Middleware 層、Router 層與 Resource 層嚴禁使用 `agent_id: str` 進行資源定位，必須統一傳遞經認證的 `AgentIdentity` 物件。
*   **路由層強制校驗**: `/ui/custom_models` 等涉及物理檔案 CRUD 的路由，必須在端點內部將 `model_id` 轉換為 `AgentIdentity` 物件後方可調用 Service 層。
*   **主權隔離 (Prison Principle)**: `ResourceManager` 根據 `AgentIdentity` 物件解析資源路徑。Agent 被鎖定在其所屬的 `Soul`, `Personal`, `Stats`, `Logs` 目錄中，嚴禁越權訪問。
*   **自主任務分派代理**: 系統對 `DispatchAgent` 的任務看板實施「能力代理 (Capability Assumption)」。如果目前發起請求的 `AgentIdentity` 在 `capabilities` 中包含 `"can_supervise_dispatch"`，`MissionManager` 將被授權繞過物理監獄，代理存取並操作 `DispatchAgent` 的個人看板。未獲授權的請求將直接被權限層阻斷。
*   **LDH 首裝限定 ⭐**: 只有系統未認領、完全未附 Authorization 的受信任本機請求可走 LDH。失效 token 一律 401；系統認領後無卡本機請求也 fail-loud，不再鑄 DebugAgent 卡（ADR-0131／0144、禁令 60）。

### 3-K. 資訊安全守衛層：對話歸屬（ADR-0276）

其他跨切面守衛見 [LEGAL_ARCH §3-K](LEGAL_ARCH.md#3-k-資訊安全守衛層-infosec-guards-)；此處登記對話歸屬的單一來源。

| 來源 | 責任與使用邊界 |
|---|---|
| `ToukaProxy/src/utils/session_scope.py` | `require_session_scope`、`can_rebind_session`、`is_empty_draft`、`is_ui_turn_base` 與 `channel_session_id`：精確綁定、家一致、退休／保留 ID、人類 UI 換綁條件及完整 JSON 座標雜湊；不自行解析身分或碰資料庫。 |
| `ToukaProxy/src/utils/async_db.py` | `_check_session_write`＋`SessionService._save_session`：正式 ID 解析、兩側授權、session 鎖＋IMMEDIATE 交易、嚴格讀取與 CAS、受管 INSERT marker=1／非空 UI 換綁降為 0；`save_ui_session`／`admit_ui_stream` 是人類換綁專用入口，`read_execution_snapshot`／`read_home_snapshot` 驗執行及家歷史，`set_home` 先驗目標再改家。 |
| `ToukaMemory/src/memory_server/protocols.py` | `IResourceProxy.query_ui_conversations` 繼承 `IConversationReader`：Memory 查 UI 的唯一接縫，傳可信 identity 與篩選／分頁參數，回 `ConversationPage`，不暴露 DB 路徑（ADR-0277）。 |
| `ToukaProxy/src/services/resource_manager.py`、`src/main.py` | 啟動時 `bind_ui_conversation_reader(session_service)`；查詢只委派，未綁回 internal_error，沿 delegates 豁免，不另加權限修復類別（ADR-0277）。 |
| `ToukaProxy/src/utils/async_db.py` 的 `SessionService.query_ui_conversations` | 普通讀取交易先驗全部家候選再讀內容；`_canonical_session_agent`／`agent_service.identify_agent` 是兩側正式 ID 來源，`session_scope.require_conversation_actor`／`is_searchable_session` 與 `require_scope` 判身分、marker、退休 ID 及綁定（ADR-0277）。 |
| `ToukaProxy/src/utils/async_db.py` 的 `get_restore_pending_path`／`SessionService` 還原方法 | 持久標記名稱與資料根位置的唯一來源；`mark_restore_pending` 經 ResourceManager 原子寫入，`invalidate_restored_sessions` 降全部列成功後刪標記。UI 搜尋讀列前檢查；startup migration 後經 `recover_pending_restore` 補降，失敗保留標記並記固定碼（ADR-0277）。 |
| `ToukaProxy/src/services/ui_conversation_reader.py` | 純樹驗證、目前祖先鏈、文字投影、內容／角色／時間篩選與分頁；不持連線（ADR-0277）。 |
| `ToukaMemory/src/memory_server/models/conversation_search.py`、`tools/legacy_vault.py` | 前者定義 row/page、固定錯誤與共用排序鍵，後者選來源、合併後切頁與包歷史界線；LegacyStore 先正規化異常時間再計數／切頁（ADR-0277）。 |

`session_scope_version` 舊列預設 0、公開投影不回傳、UPDATE 不提升；聊天搜尋須同時驗精確靈魂歸屬，不能把 marker 當授權或逐則作者證明（ADR-0277）。所有 writer 一起升級；舊共享頻道不搬移、不續寫。

### 4. 工具調用大一統 (Unified Tooling SSOT)
*   **調用模式**: 系統全面收攏為 **Command-Action Dispatcher** 模式。
*   **身份感知執行**: 所有工具執行前必須通過 `PermissionService` 的二次驗證，並強制將 `AgentIdentity` 注入操作上下文，防止參數劫持。
*   **角色授權解析（ADR-0196）**: `PermissionService.resolve_grants(agent_role)` 是原生工具／技能／MCP 的唯一有效權限解析器；allow 沿角色鏈累加、角色 deny self-only、跨角色拒絕只走 `global_denied_*` 地板且約束 Owner。`permissions.json` 不含 `auth_level`；後者屬 per-session／request ABAC／HITL，由 `auth_policies.json` 處理。
*   **模型管理主權 (Model Management)** ⭐: `ModelManager` 為模型發現與健康檢查的唯一調度中心。
    - **屬性持久化**：模型屬性（如 `enabled`, `visibility`）由持久化層保護，動態抓取的新模型僅更新 `last_seen` 狀態，不得覆蓋使用者手動設定。
    - **靈魂硬保護 (Soul Guard)**：系統強制實施「靈魂主權保護」。只要物理目錄 `data/soul/{id}` 存在，該 ID 即被視為永不離線的實體，受 Pruning 豁免保護。
    - **配置動態合併**：發現物理代理人後，系統會自動合併 `models.json` 中的顯示名稱（Name）與屬性，實現「發現即繼承」的無縫體驗。
    - **執行期路由 SSOT（ADR-0208）**：`ModelManager._custom_routes` 的不可變
      `CustomModelRoute` 是自訂模型 endpoint＋key 的唯一執行來源；整張表在探索完成後原子替換，
      不持久化、不投影 API。`provider=proxy` 是 Chat 控制面，固定排除於上游探索。
*   **供應商金鑰即時存取主權 (Provider Key Access SSOT)** ⭐ — ADR-0118（extends ADR-0099，2026-07-02）: 供應商 API 金鑰的**即時（live）**存取唯一入口為 `registry.get_key(...)` 與新增便利函式 `registry.get_key_by_id(provider_id)`；**嚴禁**於模組載入時綁定快照常數（如 `from src.config import GEMINI_API_KEY`，重載後即失效）。`dispatchers/factory.py`：gemini 分支改用 `registry.get_key_by_id("gemini")`（live）；claude 分支每請求以 `anthropic_client.with_options(api_key=registry.get_key_by_id("anthropic"))` 套用（共用 httpx pool）；OpenAI-相容供應商本即經 `get_provider_info→registry.get_key` live。
*   **供應商狀態衍生主權 (Provider Status Derivation SSOT)** ⭐ — ADR-0118（extends ADR-0099，2026-07-02）: 首裝精靈的已配置金鑰供應商清單由 `registry.discoverable_specs()`（SSOT）**衍生**，取代原本會漏掉 `deepseek`（導致已配置的 DeepSeek 金鑰恆顯示「未設定」）的硬編碼字典；金鑰值仍即時讀 `os.environ`，`tavily`（非供應商的搜尋金鑰）另行加入。

### 5. 前端狀態與設定 SSOT (Frontend State & Settings)
*   **上帝元件消除 (No God Components)**: 嚴禁在單一 View 元件中堆疊跨領域的狀態。
*   **領域資料鉤子 (Domain-Specific Hooks)**: 前端狀態必須透過專屬的 Custom Hooks 作為單一事實來源。
*   **設計代碼化與 Token 主權 (DaC)**: 視覺樣式的唯一事實來源為 `tokens.ts` (CSS 變數)。
*   **側邊欄折疊狀態 SSOT (Sidebar Collapsed SSOT)**: 側邊欄折疊狀態由 `page.tsx` 作為單一 facts 來源進行 `useLocalStorage` 持久化，並將 `isCollapsed` 狀態單向傳遞給無狀態的 `LeftSidebar` 元件，消除雙重 State 的潛在不同步風險。

### 6. 自主預算主權 (Autonomy Budget SSOT) ⭐
*   **優先級鏈**: 請求指定 (`pulse_slots`) > 全域配置 (`autonomy.json`) > 系統保底 (5 Slots)。
*   **循環攔截**: 預算耗盡時由 `RhythmService` 強制終止 `ReAct` 循環，並注入 Nudge 提示。
*   **配額保護**: 每日請求次數受 `AutonomyService` 監控，達上限後自動拒絕非同步喚醒。

### 7. 多使用者 RBAC 與身分隔離 (Multi-User RBAC) ⭐
*   **多租戶主權 (Multi-Tenancy)**: 系統區分「人類」與「代理人」。使用者透過 `users.json` (SSOT) 綁定其 `owned_agents`。
*   **硬隔離執法**: 任何對代理人資源（Soul, Memory, Vault）的請求，必須通過 `IdentityRouter` 驗證請求者是否擁有該 `agent_id` 的主權。
*   **個人檔案 SSOT**: 使用者的顯示名稱與元數據儲存於 `users.json`，由 `/v1/auth/me` 端點進行全系統同步。

### 8. 會話管理主權 (Session Sovereignty) ⭐
*   **Cookie 基準**: 全系統 `refresh_token` Cookie 必須強制設定為 `path="/"`。由 `auth.py` 統一管理發放與銷毀。
*   **憑證透傳 (CORS)**: 前端所有核心 API 請求必須開啟 `credentials: 'include'`，確保瀏覽器能正確處理跨來源會話憑證。

### 9. 部署等價性主權 (Deployment Parity) ⭐
*   **實體完整性**: 微服務 Docker 鏡像內容必須與開發環境之模組匯入鏈對等。
*   **執法**: 透過 `guard_dependencies.py` 的「Dockerfile 物理完整性檢查」功能，驗證 `Dockerfile` 是否完整包含了目錄下所有被本地匯入的 `.py` 檔案。

### 10. 架構守衛編號與順序 SSOT ⭐
*   **單一事實來源**：以 `scripts/guard_all.py` 中的 `tasks` 陣列物理順序為全系統架構守衛的唯一事實來源。
*   **編號規則**：共有 31 個架構守衛，編號必須嚴格為 `Guard #1` 到 `Guard #31`，且與 `guard_all.py` 中的索引對齊。
*   **同步要求**：所有代碼註解、控制台輸出及架構說明文件中的守衛編號必須與 `guard_all.py` 物理順序強一致。

---

> 📋 法規執法細則請見 [docs/architecture/LEGAL_ARCH.md](LEGAL_ARCH.md)。

## 🛡️ 法規即代碼摘要 (Legal Architecture — Key Prohibitions)

| 規則 | 禁令摘要 | 守衛 |
|------|---------|------|
| TID251 | 嚴禁硬編碼磁碟路徑；必須透過 `ResourceManager` | `lint_ssot.py` TID251 |
| ENV_SSOT | 嚴禁 `os.environ`/`os.system`/`os.chdir`；必須 `src.config` | `lint_ssot.py` EnvSovereigntyRule |
| ERROR_SSOT | 嚴禁 `HTTPException(detail="...")` 硬編碼；必須 `ErrorCode` | `lint_ssot.py` ErrorSovereigntyRule |
| RAISE_STRING_BAN | 嚴禁 `raise ValueError("...")` 帶字串；必須 `ToukaException` | `lint_ssot.py` RaiseHardcodedStringRule |
| TIME_BAN | 嚴禁 `datetime.now()`/`time.time()`；必須 `time_utils` | `lint_ssot.py` TimeBanRule |
| PRINT_BAN | 嚴禁業務層 `print()`；必須 `logging.getLogger()` | `lint_ssot.py` PrintBanRule |
| FETCH_SSOT | 嚴禁前端 `fetch()`；必須 `fetchTouka()` | `guard_fetch.ts` |
| LOCALSTORAGE_SSOT | 嚴禁前端 `localStorage.*`；必須 `useLocalStorage` hook | `guard_localStorage.ts` |
| BCRYPT_IMPORT_SSOT | 嚴禁非授權檔案 `import bcrypt`；白名單 `auth_service.py` | `lint_ssot.py` BcryptImportRule |
| SQLITE3_IMPORT_BAN | 嚴禁非授權檔案 `import sqlite3`；白名單三個 SSOT 入口 | `lint_ssot.py` Sqlite3ImportRule |
| CONSOLE_BAN | 嚴禁前端 `console.*`；白名單 `error_handler.ts` | `guard_console.ts` (Guard #15) |
| MODULE_BOUNDARY | 嚴禁 ToukaMemory import ToukaProxy 內部模組 | `guard_module_boundaries.py` (Guard #21) |
| ENDPOINT_AUTH | 所有非公開端點必須有 Depends 認證守衛 | `guard_endpoint_auth.py` (Guard #24) |
| XAGENTID_IDENTITY | 嚴禁前端手動注入 `X-Agent-ID` Header | `guard_xAgentId.ts` (Guard #13) |
| EVENTSOURCE_TOKEN | 嚴禁建立不帶 Token 的 EventSource 串流連線 | `guard_eventSource.ts` (Guard #14) |


## 🚦 優先級與衝突處理 (Conflict Resolution)

當多個來源發生衝突時，遵循以下優先順序：
1. **實體路徑內容** > 軟連結/虛擬目錄。
2. **.env 配置** > 實體路徑。
3. **AgentIdentity 物件** > 請求參數中的 agent_id 字符串。
4. **UNIX Epoch** > 格式化日期字串。
5. **Bridge Adapter (lib/bridge.ts)** > 手動環境偵測邏輯。
