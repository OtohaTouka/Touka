# MCP 2026 P8-B：workload OAuth 與 owner delegation 實作計畫（第四版）

> **狀態**：歷史 checkpoint；後續 repair batches 已收斂，P8-B implementation 最終提交為
> `8e9c59a5`。
>
> **限制**：單一 ToukaProxy process；無企業 IdP，enterprise authorization 維持 fail-closed
>
> **風險**：HIGH（client secret、headless egress、撤權競態、來源 delegation、crash recovery）
>
> **上位提案**：[MCP 2026-07-28 protocol migration](mcp-2026-07-28-protocol-migration.md)
>
> **架構決策**：[ADR-0207](../adr/0207-mcp-workload-oauth-single-proxy.md)

## 1. owner 已確認決策

1. P8-B 只開直接 client credentials 與 owner 明示的 heartbeat delegation。
2. 目前沒有企業 IdP；enterprise／SSO 等整個 MCP 更新完成後另案，不得以 Touka admin JWT 冒充 assertion。
3. runtime 只支援單一 Proxy process；啟用時必須明示 local filesystem 並取得 exclusive lock。
4. 直接模式只支援 `client_secret_basic`／`client_secret_post`；`private_key_jwt`、DPoP、JWT bearer 不降級。
5. owner delegation 只引用 exact P8-A active grant，不複製 token；browser 不提交 grant ID/version。

## 2. 使用者可見場景

1. Admin 建立 non-secret workload profile、一次性輸入 client ID/secret、明示 refresh metadata，API/UI 永不回填秘密。
2. Heartbeat 對 direct installation 執行 exact `tools/call`，每次送出前重驗 authority 並接受 audit intent。
3. Owner 為自己擁有的 agent 建 exact installation/tool/scope/expiry delegation；伺服器推導 P8-A source grant。
4. Foreground、dispatch、非 owner、過期/舊 generation、錯 tool/scope、enterprise 或 unsupported auth method 全部 fail-closed。
5. Profile/credential/delegation 撤銷會停止新 egress、取消並等待已開始 send；逾時保留 recovery fence。
6. P8-A source disconnect 先撤銷全部衍生 delegation；未 drain 前不得斷開 source grant。
7. 第二個 Proxy process、remote/unknown filesystem 或多 worker 拓撲拒絕啟動 workload runtime。
8. restart 後 boot-local permit/token cache 消失；durable pending transition/outbox 仍可見且資料面保持關閉。

## 3. TDD 分層

- **RED 1—模型與拓撲**：exact authority keys、profile/delegation schema、HEARTBEAT-only、single-process gate。
- **RED 2—durable state**：加密 credential、metadata/delegation mirror、transition/revocation/rotation FSM、audit outbox。
- **RED 3—egress**：hardened metadata/token endpoint、兩種 client-secret method、pre-egress audit、single-use permit、task-exit ack。
- **RED 4—授權接線**：direct/owner router、immutable ticket、args digest、handle partition、source disconnect fence。
- **RED 5—控制面**：admin/owner API、server-derived source authority、secret-free projections、OpenAPI/generated types、UI/i18n。
- **RED 6—運維**：feature gates、lifespan zero-I/O baseline、backup exclusion、restore collision、key rotation/recovery。

每層先觀察預期紅燈，再做最小 Green；不得以 test-only 分支、寬鬆 fallback、時間到期推定完成或
mock 掉核心 authority fence 取得綠燈。

## 4. SSOT 與不變式

- installation policy：`server_config.json` 的 `auth_mode`、`workload_authority_kind`、`workload_delegation_id`。
- non-secret policy：workload profiles/delegations JSON，generation CAS＋ResourceManager atomic write。
- secret/runtime：專用 workload SQLite＋專用 key ring；`data/` 永遠不參與。
- invocation：tool ticket 只接受實際 wire `tools/call`，綁 raw tool、args digest、authority/generation/
  context，單次且短 TTL；transport bootstrap 使用獨立 authority。
- egress：audit acceptance → permit issue → final authority recheck → begin send → task exit/cancel ack。
- final OAuth wire：client-credentials／delegated refresh 先凍結實際 `POST` endpoint、form shape、resource、
  scopes、client-auth method 與 durable generation，再以不含秘密的 canonical digest 綁 audit/permit/send。
- mutation：唯一 coordinator 擁有 mutation lock 與 exact global fence；durable transition/revocation/
  rotation blocker 直接進 permit admission，epoch commit 後不得在 finalize/drain 前 reopen。
- recovery：indeterminate transition 永不自動放行；admin recovery API 統一顯示 transition、pending
  revocation 與 rotation minimal state。transition 只接受明示 `confirm_present`／`confirm_absent` 並由
  server 核對 durable operation＋JSON/SQLite；drain 只可明示 retry/finalize。
- replay/API：consumed tool ticket index 固定容量並按 expiry prune；owner delegation response 不含
  P8-A source slot、grant ID/version 等 internal binding。

## 5. 驗證與交付閘

1. 聚焦 pytest、Ruff、frontend Vitest/ESLint/typecheck。
2. `scripts/guard_all.py`、完整 `pytest tests/`、完整 ToukaChat `npm run test`。
3. OpenAPI/generated types、ADR/SSOT/interface/security/tests README、Compose config 同步。
4. 未經 owner 確認前不勾 P8-B、不發布、不啟用；AI 不 stage、不 commit。

## 6. 回滾

- 先把 `TOUKA_MCP_OAUTH_WORKLOAD_ENABLED=false` 與 `TOUKA_MCP_OAUTH_DELEGATION_ENABLED=false`，
  重啟單一 Proxy；保留加密 store、policy 與 key ring供稽核，不自動刪除。
- rollback 不把 workload secret 搬入 P8-A store，也不把 delegation 改成 static header。
- 若需 restore，必須先由 owner 明示處理既有 P8-A/P8-B store/key collision；工具固定 fail-closed。

## 7. 延後項目

- Enterprise-Managed Authorization／SSO、`private_key_jwt`、DPoP、JWT bearer。
- 多 worker／多主機 durable distributed permit 與 fencing。
- foreground/dispatch workload delegation，以及任何 durable MRTR/task continuation。

## 8. 第 2 輪第四版修復（2026-08-03）

1. RED 證明 durable blocker 未進 permit admission、global fence 缺 exact completion token、profile delete
   epoch advance 後可能 reopen、same-generation metadata refresh 衝突。
2. RED 證明 token exchange permit 綁到 MCP arguments、delegated refresh guard 無 final request binding，
   consumed ticket replay set 無容量與 expiry prune。
3. RED 證明 control/rotation 沒有單一 fence owner、transition 無可操作的 typed reconcile、owner API
   暴露 P8-A source authority internals。
4. Green 以單一 mutation coordinator、durable readiness、immutable OAuth wire binding、bounded heap index、
   same-generation metadata replacement、explicit transition reconcile 與 owner-safe projection收斂。
