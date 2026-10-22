# 更新歷史：2026 年 9 月：來源功能交付

> 本檔保存原 CHANGELOG 的歷史紀錄，保留當時的 PASS／FAIL／UNKNOWN／NOT_RUN／HOLD／REUSED_NOT_FRESH 與限制；舊「current」、批准與命令不代表現在可執行。唯一現況見 [HANDOVER](../../HANDOVER.md)，有效規範見 [AGENTS](../../AGENTS.md)；返回 [更新摘要](../CHANGELOG.md)／[歷史索引](README.md)。

> 本檔是開源前的歷史紀錄；文中的 PR 編號、commit 與 run 編號指的是私人歷史。依各紀錄明示日期與階段分類，檔內維持原文相對順序。

---

<a id="entry-0001"></a>

## 2026-09-21：模式與登入來源修正（完整來源回歸通過／未發布）

- 模式改依共用 Proxy URL 與通過驗證的 Memory 能力，明確區分未知、認證、降級與 hybrid，無新增輪詢。
- 登入顯示固定安全分類；token/profile／origin／操作世代的晚到結果不覆蓋新 client 狀態。
- 初始管理員密碼交付失敗不再提交 users；既有 admin 與密碼雜湊政策保持。
- 真實管理員原因／恢復仍未確認，GUI 延期；完整 Python 10050P／9skip／5warnings、Chat 1464P；結果與限制的證據未入版控，未提交／部署。


<a id="entry-0002"></a>

## 2026-09-21：可靠性 R2（來源實作完成／未發布）

- 任務受管保存不明傳固定錯誤，保留權限、review與鎖；對話採strict snapshot／SQLite CAS。
- 明示UI typed終態、固定節點、trim條件提交與buffer owner／generation隔離；外部wire保持。
- Chat純writer、scope清除、保守刪除、富樹保留／核對／單次僅保存重試；無痕零持久化。
- 真Python producer／TS decoder／writer／owned SQLite共用fixture，保留JSON擴充與presence。
- AnyIO因fresh audit發現升精確4.14.2；沒有Git提交、發版或live。9/16的原始證據已遺失、不重建；9/21的證據獨立保存（未入版控）。

完整Python R4通過10048P／9個既有條件skip／5warnings，2464來源inputs＋6runtime前後零漂移；最終驗證與102檔材料的證據未入版控，最終審查以當次審查紀錄為準。Chat1409P與其他適用既有驗證按hash重用，43非audit及兩audit回讀分列；未提交、發布或部署。
Windows cleanup兩次FAIL及其後NOT_REPRODUCED保留，歷史原因仍UNKNOWN。續行時以官方API契約與零child反例證實ReadFile109的EOF分類缺漏，正式5F／9P→14P、整檔700P及完整R4通過；不把語意修正冒稱兩次歷史失敗已唯一歸因。


<a id="entry-0003"></a>

## 2026-09-16：發布前 F02–F05 與自動化安全來源（來源實作與驗證完成）

- Heartbeat strict storage、原子提交與精確jobs補償；保留舊設定presence，失敗明示degraded／UNKNOWN。
- 真人排程經Proxy fresh auth／agent擁有權與revision CAS；Chat保留草稿、安全錯誤及「成功但刷新失敗」區分。
- 單一TokenProvider接管理／trigger／outbound，fallback不假裝共享就緒；A→B→C fresh imports驗證重啟一致性。
- source Compose四個host bindings改loopback，保留埠覆寫／內部通訊；分段invocation讀共同設定。
- 日常source CI核心必跑、前端保守判定、精確runtime及原件upload；只到SOURCE_READY，hosted未執行。
- Guard32／37限出貨範本與明列宣告；standalone smoke強制owned HB／data與首個日誌隔離。
- B1–B4及B5來源階段接受，B6必要驗證完成（full 9862P／9skip／5warnings，425.71秒，43guards／24smoke／Chat1241P），最終審查紀錄未入版控；下方原生工具44檔及發布記錄是歷史，未被撤回或重做。


<a id="entry-0004"></a>

## 2026-09-16：發布前 F01 可信工具批准分類（來源候選）

- 以 immutable callable binding 區分普通工具、真終端及三個自帶批准工具，額外 `command` 不再改道；批准後執行同一已解析 handler。
- 保留 RoleGrants、PaC、HITL、七工具公開合約，補 sandbox 真寫入與分類漂移負例；Guard45 增加有限 binding AST 接線驗證。
- pytest 兩份權限 seed 僅讀 tracked examples，缺檔明確失敗；不讀取開發者私有設定作基線。
- B1 focused 238P，整體 B6 尚待；無 Git mutation、發布或 live 變更。原失敗與更早驗收歷史保留。


<a id="entry-0005"></a>

## 2026-09-15 — 原生工具公開合約修復候選

Memory emotions/date/forget ID/manage_dups、Mission初始化review/evidence、evolution驗證quota、create_skill author完整揭露；六組條件/描述對齊原實作。
七typed public contracts統一schema/驗證，Guard45＋PR source job＋provider/backend負例防漏；R2補native實際名稱RoleGrants執行閘，無角色/deny不得透過PaC/HITL放行。
R3同批修正Memory忽略已明示env來源的既有缺口；明示後不fallback，原factory/audit回歸保持。
research三欄位及handler/schema不改；其既有角色授權受通用native執行閘保護。來源驗證完成：full9604P/11skip/5warnings，兩個HB條件skip另synthetic11P補驗；fresh43非audit與24smoke通過。P1–P6來源計畫完成，**已通過最終審查**；沒有commit/release/live。


<!-- End of archived source entries. -->
