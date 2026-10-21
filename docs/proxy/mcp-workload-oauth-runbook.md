# MCP workload OAuth 單一 Proxy 運維手冊

> 本手冊對應 ADR-0207。P8-B implementation 已提交為 `8e9c59a5`；這不代表 feature flags 已啟用，
> 也不擴張至 enterprise／SSO、foreground、dispatch 或其他未支援模式。

## 啟用前檢查

1. Proxy 啟動命令只有一個 server process，未設定 `--workers`，同一 config 目錄沒有第二個 Proxy。
2. config volume 是本機 filesystem；設定 `TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND=local`。無法證明為 local 時維持 `unknown`，讓啟動 fail-closed。
3. 先保留 `TOUKA_MCP_OAUTH_WORKLOAD_ENABLED=false`、`TOUKA_MCP_OAUTH_DELEGATION_ENABLED=false`，由 admin 建立 profile/credential，再逐一開啟所需模式。
4. MCP installation 必須明示 `auth_mode=oauth_client_credentials` 及 `workload_authority_kind=direct` 或 `owner_delegation`；enterprise/其他方法不會自動降級。

## 正常操作順序

1. Admin 在設定頁建立 non-secret profile。
2. 以一次性 password 欄位送 client ID/secret；確認 credential status，不期待 API 回傳秘密。
3. 明示執行 metadata refresh；API 只回 profile/generation/verified time/status。token endpoint、
   CIDR、scope 與完整 snapshot 僅留在 AES-GCM store，不以 UI/API 揭露。
4. Direct 模式啟用 workload feature。Delegation 模式另由 owner 對自己擁有的 agent 建 exact tool/scope/expiry policy，再啟用 delegation feature。
5. 觀察 recovery 清單與 key rotation 狀態為 stable；任何 pending/recovery row 都先處理，不得把 feature gate 當繞過手段。

## 故障判讀

- **local filesystem topology error**：filesystem kind 未設 local，或 config volume 無法證明為本機。不要改成寬鬆 auto-detect。
- **runtime lock busy**：另一個 Proxy process 已持有 P8-B lock。停止額外 process；不得刪 lock file後同時運行。
- **recovery status 非空**：JSON/SQLite mutation、revocation 或 rotation 曾在 fence 中斷。保存現場並依 recovery ID／authority generation 對帳；task 已確認退出後，以
  `POST /v1/admin/mcp/oauth/workload/recovery/retry` 明示重試。只有 explicit finalize 後項目才消失。
- **transition 類 recovery**：先讀 `GET /v1/admin/mcp/oauth/workload/recovery`，以回傳的 opaque
  `recovery_id` 對帳 non-secret profile/delegation policy。若實際目標應保留且 durable generation/digest
  完全吻合，對 `POST /v1/admin/mcp/oauth/workload/recovery/transitions/{recovery_id}/reconcile` 送
  `{"resolution":"confirm_present"}`；若 reduction 的安全結果應為不存在，送
  `{"resolution":"confirm_absent"}`。endpoint 不接受 caller 自述 generation/digest，會由 server
  對 exact operation、JSON 與 delegation SQLite mirror 重驗。不確定時不得試兩個選項、不得改 DB；
  保留 fail-closed 狀態並進行人工調查。reconcile 後若仍有 revocation/rotation blocker，再執行 retry。
- **audit backlog 達上限**：新 permit 會被拒絕。先恢復 audit sink/drainer，確認 outbox ack 後再重試。
- **source disconnect 失敗**：仍有衍生 delegation send 未退出；等待 task cancellation/exit ack，再重試同一 disconnect。不要直接刪 P8-A grant。

## Key rotation

只從 admin rotation API 啟動 managed rotation。流程會停止新 egress、取消/drain、重加密並 retire 舊 key；external key source 不產生 key material。任何 drain timeout 都視為未完成，保留舊 key 與 recovery fence。

## API 最小揭露

Owner delegation list/put response 只包含 delegation ID、execution agent、installation/generation、tool、
scope、generation/status 與時間；不會回傳 owner internal ID、P8-A source slot、source profile/grant
generation 或 grant ID。診斷這些 internal binding 必須在受控 server-side audit/recovery 流程完成，
不得要求前端回填。

## 備份與還原

一般備份保留 non-secret profile/delegation JSON，但排除 `mcp_oauth_workload.sqlite3`、WAL/SHM 與全部 `TOUKA_MCP_OAUTH_WORKLOAD_KEY*`。目的地已有 P8-A/P8-B store 或 key 時 restore 固定回 `MCP_OAUTH_RESTORE_CONFLICT`；不得自動覆寫、刪除或合併。

## 安全停用／回滾

把兩個 P8-B feature gate 設為 false 並重啟唯一 Proxy。這會拒絕新 workload/delegation egress，但保留加密 state 與 audit evidence。不要把 client secret 改存 static headers，也不要移動或刪除 `data/` 下任何內容。
