# MCP 2026 P0 Cutover Inventory

> **歷史紀錄**：基線 2026-07-30；runtime source commit
> `c6aebfe1f490260b0a798e0e1d54fdfcdd2c413b`。原逐 finding 盤點腳本與 v1 rollback smoke
> 已依 ADR-0238 刪除，本頁只保留 P0～P4-B 的歷史結論，不能再重跑；現行 MCP 為
> modern-only（ADR-0221）。

## 結論

- 掃描 1707 個 tracked／未忽略原始檔與明示 config 檔。
- 261 個已分類 finding，其中 86 個阻擋 P2；0 個 unclassified。
- `ToukaProxy/config/server_config.json` 與 `.example` 都是空 registry；
  `ToukaProxy/config/mcp_servers/` 不存在，因此本機 generated-artifact blocker 為 0。
- `data/` 永遠排除；掃描器與測試沒有讀寫實體 `data/`。
- 當時 P2 gate 必須失敗；P1／P2 逐項消除 blocker，而不是建立例外 allowlist。

## P1 後現況（ADR-0198）

- 掃描 1713 個檔案、277 findings、96 blockers、0 unclassified。
- 四個跨 principal blocker
  (`security.private_catalog.identityless`、`security.call.identityless`、
  `security.handle.global_server_key`、`security.tool_result.global_cache`) 已全部歸零。
- P1 新增 legacy adapter／handle 與 v1 characterization 測試，因此掃描器多看見
  14 個待 P2 同批處置的 SDK-v1 anchor：`python.mcp_import.v1` 8→11、
  `python.client_session.v1` 16→18、`python.camel_case.v1` 5→14；扣除已消除的
  4 個安全 blocker 後，總 blocker 86→96。這不是 modern wire 已回退，而是 P2
  原子 cutover 必須一併更新 adapter 與測試的顯式清單。
- P2 gate 仍正確回 exit 2；目前沒有 security 類 blocker，但 SDK／transport／IDE
  blocker 尚未清零。

## P4-B 後現況（ADR-0202）

- P2、P4-A 與 P4-B phase blocker 均為 0；全域 `cutover_blockers()` 亦為 0。
- ToukaIDE 已移除 Node v1 umbrella SDK，改用精確鎖版的 v2 分拆套件；legacy
  HTTP+SSE 只分類為 `node.legacy_compat` 非阻擋 finding。
- 掃描器會攔截 v2 package 缺席、非 exact patch、manifest／lock 不一致、舊 server
  API 與缺 test script；不得以移除 legacy 相容窗來換取假綠。
- ToukaChat 的 transitive Node v1 lock 仍是 `review` finding，不屬 ToukaIDE
  runtime，也不阻擋 P4-B。
- `data/` 排除規則未變；P4-B 實作與驗證均未讀寫實體 `data/`。

## Blocker 分類

| code | 數量 | 主要處置 phase |
|---|---:|---|
| `python.dependency.v1` | 3 | P2：Proxy／Dispatch 升 v2，Memory 證無 import 後移除 |
| `python.lock.v1` | 1 | P2：正常 `uv lock` 重生 |
| `python.mcp_import.v1` | 8 | P2 原子 import cutover |
| `python.fastmcp.v1` | 10 | P2：Dispatch／Grok／Creation／test stub |
| `python.client_session.v1` | 16 | P2：client／Creation smoke／test doubles |
| `python.initialize.v1` | 5 | P2：移除手動 initialize |
| `python.streamable_http.v1` | 9 | P2：v2 Client target／HTTP factory |
| `python.camel_case.v1` | 5 | P1 canonical boundary＋P2 SDK model |
| `python.legacy_sse` | 1 | P4-A：Dispatch 雙掛與 sunset |
| `node.sdk.v1` | 1 | P4-B：ToukaIDE SDK v2 |
| `node.lock.v1` | 1 | P4-B：ToukaIDE lock 重生 |
| `node.server.v1` | 2 | P4-B：Node v2 server API |
| `node.legacy_sse` | 5 | P4-B：`/mcp` 主路徑＋legacy 雙掛 |
| `node.camel_case.v1` | 14 | P4-B：ToukaIDE wire/model 遷移 |
| `node.tests.missing` | 1 | P4-B 前建立 IDE protocol test gate |
| `security.private_catalog.identityless` | 1 | P1：private catalog 綁 principal |
| `security.call.identityless` | 1 | P1：強制 `McpRequestContext` |
| `security.handle.global_server_key` | 1 | P1：installation＋principal handle key |
| `security.tool_result.global_cache` | 1 | P1：移除 `tools/call` 完成結果快取 |
| **合計** | **86** | |

非 blocking 盤點另涵蓋 ToukaChat 的 transitive Node v1 lock、31 個 MCP monkeypatch
target、12 個 MCP/httpx surface、8 個 deployment/release surface 與 122 個文件面。

## 高風險實作面

| 面向 | 目前檔案 |
|---|---|
| Python dependency | `ToukaProxy/pyproject.toml`、`ToukaDispatch/pyproject.toml`、`ToukaMemory/pyproject.toml`、`uv.lock` |
| Python client | `ToukaProxy/src/protocols/mcp/client.py`、`ToukaProxy/src/protocols/manager.py` |
| 自有 Python server | `ToukaDispatch/server.py`、`ToukaProxy/src/tools/grok_imagine_server.py` |
| Creation | `ToukaProxy/src/services/creation/scaffold.py`、`smoke.py`、`mcp_kind.py` |
| ToukaIDE | `ToukaIDE/package.json`、`package-lock.json`、`src/mcp/server.ts`、`src/mcp/tools.ts` |
| 發布 | compose 檔、Proxy／Heartbeat／Chat Dockerfile |

## Rollback 狀態（歷史）

P0～P4-B 期間曾以 Owner 明示接受的本機 RepoDigest 保存 v1 rollback 並完成一次 sandbox live smoke；
該 smoke 腳本已刪除。P0 至 P4-B 已收官，現行 MCP 不提供 v1 rollback。
