# ADR-0198：MCP SDK-neutral 邊界與 principal handle 隔離

> C2-3 現況補註：§5 的白名單仍是環境底；包住的 stdio 將條目 env 分到 spec.exec_env，限制套完才交給目標，direct 照既有 production／clean；principal-scoped stdio 在 owner 建立前拒絕。 詳見 ADR-0275。

- **狀態**：Accepted
- **後續修訂（[ADR-0254](0254-iso-c1-small-fixes-and-execution-gaps.md)）**：§5 的 production 改成白名單底（`LAUNCH_ENV_ALLOWLIST`）＋條目 env＋`TOOL_BASE_URL`，不再組完整必要環境；clean 用同一個白名單＋spec env，不再收 clean base factory。
- **日期**：2026-07-30
- **相關**：[ADR-0191](0191-mcp-hub-tier-a-shelf-and-version-pinning.md)、
  [ADR-0194](0194-mcp-remote-shelf-and-manual-remote-entry.md)、
  [ADR-0197](0197-mcp-2026-p0-cutover-baseline.md)、
  [MCP 2026 完整遷移計畫](../proposals/mcp-2026-07-28-protocol-migration.md)

## 脈絡

P0 證明現行 MCP runtime 有四個跨 principal 風險：工具列舉沒有 request identity、
`call_tool_on_server` 沒有 request context、handle 只以 server name 定址，以及
`tools/call` 完成結果會依 server/tool/args 跨 request 快取。直接升 Python SDK v2
不會自動修復這些語意；反而會讓 OAuth、Tasks 與 elicitation 共用錯誤的身分邊界。

現行 runtime 仍是 Python SDK `mcp 1.26.0`。P1 因此只建立不依賴 SDK 欄位命名的內部
合約，並用 legacy adapter 承接 v1；不宣稱已支援 MCP `2026-07-28` modern wire。

## 決策

### 1. 所有 MCP 操作先進 canonical boundary

新增不可變 `McpRequestContext`，至少保存 request/session、已驗證 `AgentIdentity`、
原始 human principal、installation、auth level、來源、政策、取消與 trace context。
`call_tool_on_server` 的 `request_context` 是必填 keyword-only 參數；`ToolExecutor`
只能由已驗證的 pipeline identity 建立它。

工具描述與結果統一為 SDK-neutral `McpToolDescriptor`／`McpOperationOutcome`。P1
legacy adapter：

- 保存 title、input/output schema、annotations、icons、meta 與 content；
- 缺少 `resultType` 時只可解讀為 `complete`；
- `structured_content` 只承諾 v1 可證明的 object 或 `None`；
- `is_error=true` 是合法 tool result，不冒充成功，也不轉成 HTTP 500。

文字投影只允許在 `ToolExecutor` 的模型輸出最末端發生。scalar／array／null
structured output、MRTR 與 Tasks 僅先有 canonical 形狀，wire conformance 留待後續
phase。

### 2. protocol、transport 與 tool-result error 分流

JSON-RPC `McpError` 保留原始 code/data，再經唯一 mapper 對映 Touka `ErrorCode` 與
模型可讀的 tool error class；transport/runtime error 走另一分支並先消毒訊息。
`is_error=true` 不丟 exception，而是回結構化工具錯誤，讓模型可修正輸入。

### 3. installation 與 runtime handle 分離

`InstallationState` 只保存安裝層設定、generation、restart 與公開健康資訊；不得保存
可變的「目前使用者」。`HandlePool` 以
`installation_id + principal_key + protocol_mode + config_generation` 定址：

- `none`、`static_headers`、`oauth_client_credentials` 可使用 shared handle；
- `oauth_user`、`enterprise` 必須依原始 human/delegation principal 分割；
- principal-scoped installation 啟動時不連線，只能在帶精確 request context 的呼叫
  或列舉時 lazy connect；
- identity-less catalog 不列 private installation，也不聚合其他 principal 的工具。

principal handle 的連線錯誤與 circuit breaker 不得污染 installation 或其他 principal
的健康狀態。restart／cleanup 以 installation 為範圍關閉全部 handles，generation
遞增使舊 key 失效。

### 4. `tools/call` 不快取完成結果

刪除跨 request 的 `ToolCacheEngine` 與完成結果快取。相同 server/tool/args 的兩次
呼叫必須各自執行，避免副作用被錯誤去重。P1 不加入 in-flight coalescing；未來若要
加入，只能限同 identity、同 request lifecycle，並於 task 完成立即移除。

### 5. 子進程環境有兩種明確政策

`LaunchEnvironmentPolicy.production` 組合正式 runtime 必要環境、條目 env 與
`TOOL_BASE_URL`；`LaunchEnvironmentPolicy.clean` 只組合 clean base 與 spec 明示
env。Creation candidate 永遠走 clean policy，不繼承 Touka、Dispatch、Heartbeat
或 provider secrets。

### 6. 遷移設定契約同批對等

`ProtocolServerConfig` 與 API `MCPServerItem` 同時增加：

- `protocol_mode`: `auto | legacy | 2026-07-28`，P1 預設 `legacy`；
- `auth_mode`: `none | static_headers | oauth_user | oauth_client_credentials | enterprise`；
- `auth_profile_id`、`capability_policy`、`sse_compat_enabled`。

舊 remote 條目帶 headers 且未設 `auth_mode` 時，runtime 保守推導為
`static_headers`。舊版 UI 儲存時若省略新欄位，路由必須從既有設定 carry over，
不可用 schema 預設覆寫 OAuth／protocol policy。

## 結果與邊界

- P0 的四個跨 principal blocker 已歸零；P2 inventory 仍因 SDK v1 dependency/import
  與 legacy adapter/test anchors 正確維持 fail-closed。
- P1 新增 15 個 boundary 測試，另補 config carry-over 與 inventory AST 回歸；MCP
  相關回歸 117 個測試全綠。
- OpenAPI 與 ToukaChat 生成型別已同步。
- 終態 pytest 4056 passed／4 skipped、vitest 1125 passed／220 files；44 道 guards
  除既知 #23 npm audit 外全綠。
- `auth_mode`／`protocol_mode` 現階段是 handle/config 骨架，不代表 OAuth 流程或
  MCP `2026-07-28` negotiation 已實作。
- Python runtime 仍固定 SDK v1；P2 必須原子切換 dependency、client、Creation 與所有
  v1-only import，不能以 `protocol_mode=legacy` 冒充 package rollback。
