# ADR-0206：MCP 互動式 OAuth authorization-code

- **狀態**：Accepted
- **日期**：2026-08-01
- **相關**：[ADR-0198](0198-mcp-sdk-neutral-boundary-and-principal-handle-isolation.md)、
  [ADR-0204](0204-mcp-cache-subscriptions-and-boundary-hardening.md)、
  [P8-A 實作計畫](../proposals/mcp-2026-p8a-interactive-oauth-plan.md)

## 脈絡

MCP remote installation 原本只有 `none` 與 `static_headers`。互動式使用者 OAuth 若直接
把 token 放進 server config、process cache 或一般 HTTP client，會破壞 human＋soul
隔離、跨 worker CAS、SSRF／DNS rebinding 防線，以及 callback state 一次性語意。

## 決策

1. P8-A 只支援前景 human 啟動的 authorization-code＋PKCE S256；client credentials、
   enterprise、heartbeat delegation、DPoP 與 durable continuation 延後至 P8-B。
2. 唯一 durable SSOT 是 `config/mcp_oauth.sqlite3`：AES-256-GCM envelope、HMAC lookup、
   slot/grant/registration version、lease/fence 與 durable clock watermark。raw state 只進
   authorization URL 與 inbound callback；資料庫只保存 digest。
3. grant key 綁定 human principal、execution soul、installation/generation、profile/
   generation 與 delegation digest。metadata／CIMD／DCR、refresh、bearer resource 與
   callback token exchange 前均重驗 feature、installation enabled/generation、human、
   MCP grant 與 owner authority；撤權會 freeze exact grant，不能只依 OAuth scope。
4. callback 由全域 policy 精確綁 route、method 與 response mode。預設只開 `form_post`；
   `query` 需明示 ingress canary。parser 對 body、欄位數與單欄 bytes 早期限流；query
   的成功、parser/runtime 失敗與 runtime unavailable 都回 secret-free HTML，先以
   `history.replaceState` 清除 Touka callback 當前 entry。只有成功頁可 postMessage opaque handle。
5. OAuth metadata/token/DCR/revocation 與 bearer MCP resource 使用專用 HTTPS transport：
   禁 redirect/proxy/HTTP2 coalescing，每次 connection 驗全部 DNS answers 並釘 IP。私網
   例外必須同時匹配 profile 的 installation＋exact HTTPS origin＋CIDR。CIDR 不快照進
   長生命 transport handle，而由 profile lock 內的 request-time credential 每次投影。
6. profile mutation 與 credential egress 共用 per-profile cross-process AsyncFileLock；
   SQLite `updating` intent 與 reconciler 在 crash 後 fail-closed。此為鎖邊界定義點，故
   `keyring.py` 與 `profile.py` 各有一處 `FILELOCK_ASYNC_SSOT` 精確豁免。
7. managed key rotation 採 persisted FSM：prepare → dual-read/new-write → drain → retire；
   external source 只能觀察，不能由 API 產生或回傳 key material。worker convergence 只
   snapshot lease 尚存活的 worker；liveness 至少涵蓋三個 maintenance interval，過期 row
   不得永久阻擋 retirement，重新加入者仍須先載入 authoritative ring。

## 結果與邊界

- `oauth_user` installation 改為 request-context lazy handle，且 bearer request 完成前不釋放
  profile fence；registry 沒有 enabled `oauth_user` 時不載入 OAuth key／DB，`none`／
  `static_headers` 舊路徑零 OAuth I/O。
- encrypted durable row 的格式、AEAD 或模型驗證失敗統一 fail-loud 為
  `OAuthStoreCorruptionError`，不得以一般 validation/upstream error 繼續。
- DCR crash-after-response 仍可能在 authorization server 留下 remote orphan；本機只允許
  一個 authoritative registration，且不保存 registration management credential。
- Scenario 8 只保證 raw state/code/token 不留在 Touka 控制的 storage、log 與 callback
  當前 history entry；authorization server／瀏覽器管理的跨來源 history 不在 Touka
  可控制範圍。
- `data/` 不參與此功能；OAuth DB、profile 與 key metadata 只位於 config boundary，備份
  預設排除 DB/WAL/SHM 與所有 key/rotation secret。

## 驗證

- unit/integration：callback、PKCE、metadata、scope、DCR、refresh、rotation、backup、
  logging、兩個 Windows spawn multiprocess lease oracle 與完整 mock AS flow。
- frontend：Vitest hook/component 測試與 Playwright Scenario 8 oracle。
- deployment：真 `nginx -t`、兩個 exact callback location logging canary，以及零掛載、
  `network=none` 的精確 Proxy image lifespan／health／OAuth import／OpenAPI parity smoke。
- shipped Uvicorn entrypoint 回歸鎖同時覆蓋 Dockerfile、PowerShell、Python main、root
  Compose 與 standalone Proxy Compose，避免 command 覆寫重新啟用 access log。
- 單一入口：`scripts/verify_mcp_oauth_p8a.py`。
