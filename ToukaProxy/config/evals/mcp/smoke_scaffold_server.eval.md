---
id: smoke_scaffold_server
kind: mcp
ok: true
scored: false
score: null
detail: 1 題唯讀評測基線
task_count: 1
warnings: []
tasks:
- id: smoke_scaffold_server-t1
  title: 工具 add_numbers 結構驗證
  description: 描述對 'add_numbers' 的一次安全唯讀呼叫（依 schema 填最小參數；本基線不實際執行）。
  expectation: server 'smoke_scaffold_server' 暴露工具 'add_numbers'，其 inputSchema 與宣告一致。
  probe: add_numbers
---
# Eval Baseline — smoke_scaffold_server

## Task smoke_scaffold_server-t1 — 工具 add_numbers 結構驗證
- probe: add_numbers
- expectation: server 'smoke_scaffold_server' 暴露工具 'add_numbers'，其 inputSchema 與宣告一致。

描述對 'add_numbers' 的一次安全唯讀呼叫（依 schema 填最小參數；本基線不實際執行）。
