# ADR-0121: 演化管線 4→2 合併（Evolution Pipeline Merge with State-Machine Ordering）

## 狀態

✅ **已接受（實作完成、全綠——guard_all 37/37、後端 pytest 2611 passed/4 skipped）**。本 ADR 落地 [ADR-0119](0119-unified-tool-interface-redesign.md) 的最後一個遞延項，沿用其雙軌 shim 原則；與 [ADR-0120](0120-async-command-approval.md) 同屬工具介面重設計的收尾系列。

## 日期

2026-07-04

## 背景

代理人自主演化（生成新 MCP 能力）是 4 顆連續小工具：`propose_new_capability` → `write_mcp_implementation` → `verify_mcp_locally` → `request_mcp_creation`。模型必須記住精確順序、每步一個往返；順序錯誤只得到含糊文字錯誤（如直接 write 一個沒提案過的 skill_id 會照寫不誤）；「實作完忘記驗證就送審」也擋不住（送審步只檢查 server.py 存在、不看驗證結果）。提案（工具介面重設計 §7）定案：合併為 2 段、**狀態機驗序**強制順序。

## 決策

1. **`evolution_define`**（廣告）＝提案段：委派既有 `propose_new_capability`（零重複）；其成功訊息的下一步指引改指 `evolution_implement_and_verify`（舊名已不廣告，指舊名＝幻覺誘因）。
2. **`evolution_implement_and_verify`**（廣告）＝實作＋驗證段，一次完成「寫入 server.py → 獨立 venv → pip 依賴 → 運行時監獄沙盒驗證」；參數 `skill_id, code, test_tool_name, test_arguments`（必填）＋ `dependencies, submit_for_deploy`（選填）。可反覆呼叫迭代修正。
3. **狀態機驗序**（機械強制、非約定）：
   - 未 `evolution_define`（無 spec.json）→ 結構化 `validation_error`＋指路，不動任何檔案；
   - 環境建置失敗 → 立即回報、不執行驗證；
   - `submit_for_deploy=true` 僅在**本次驗證通過**（沙盒回 `"status": "success"`）時觸發既有 HITL 部署（`request_mcp_creation`：使用者審核 → server_config.json → 熱掛載）——驗證未過**絕不部署**、驗證通過但未要求部署則提示下一步。
4. **輸出衛生**：合併輸出剝除 `write_mcp_implementation` 的舊導覽尾句（「Next, use 'verify_mcp_locally'…」）——指向未廣告 shim 的殘句是幻覺誘因；委派時剝除重疊鍵（模型幻覺多給 `tool_name`/`arguments` 等不炸 TypeError）。
5. **雙軌**：舊四步保留為可執行 shim（不再廣告；`LEGACY_SHIM_SCHEMAS` 存形狀文件、smoke `SCHEMA_EXEMPT` 同步）。廣告面 29→**27**。`CRITICAL_TOOLS` 納入兩新名。

## 後果

- 演化流程從「模型記 4 步順序」變成「define → implement_and_verify（迭代）→ 加旗標送審」；順序錯誤得到可自我修正的結構化錯誤；「未驗證就部署」在機制上不可能。
- 部署審核仍為同步 HITL（`request_approval`）：部署是管線終點、模型當下無其他事可做，維持同步可接受；若未來成為痛點可套 ADR-0120 的 approval_pending 模式。
- eval（select-only、8 案例、廣告 27）：grok-4.3 **100%（選錯 0%）**、deepseek-v4-flash（n=3）**83.3%（選錯 5.3%）**——與合併前最佳水準持平，廣告面縮減零回歸；deepseek 唯一殘留仍為慢性 `mission-initialize`（與演化無關）。
- 工具介面重設計至此**全部收尾**：廣告面 21（改造前）→ 27（9 動詞＋edit_soul＋send_message＋execute_command＋get_command_result＋evolution 2 段＋維持工具），內藏 ~30 個 action 組合的 6 顆 god-tools 全數退為 shim。

## 相關

- [ADR-0119](0119-unified-tool-interface-redesign.md) — 原生工具介面重設計（本 ADR 為其最後遞延項）
- [ADR-0120](0120-async-command-approval.md) — 指令執行非同步審批（同系列收尾）
- [ADR-0076](0076-evolution-sandbox-os-resource-limits.md) — 演化沙盒 OS 級資源限制（監獄機制不變、完整沿用）
- 剩餘遞延（另案）：多回合 eval、舊 shim 軟下架時機
