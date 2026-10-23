# ADR-0143: HITL 全面統一——四級決策表 × 工具風險分類 × 聊天內審批 broker

## 狀態

✅ 已接受（H1–H4 全數落地，2026-07-11 同日）

> 🔧 **四級決策表已被 [ADR-0246](0246-model-layer-stage-2b-2c.md)（Manual／Auto／YOLO 三模式）修訂**：4×3 表改成三模式表（Auto 的終端與 dangerous 工具回 `REVIEW` 交審查員，底線回 `HUMAN_ONLY`）；終端不再依 `safe_whitelist` 分類、一律 dangerous；心跳的四級欄位改收三模式。`tool_risk`、CONFIRM fail-closed、聊天審批 broker 與 `/v1/tools/approve|reject` 的人類硬閘不變。

T 系列（思考分頻＋前端摺疊）為同提案的獨立第二目標，另立 ADR（見
[提案](../proposals/hitl-unification-and-reasoning-fold.md)）。

## 日期

2026-07-11

## 背景

Sumi（grok-4.5）診斷揭露 HITL 整條斷裂——使用者選 Manual，工具調用卻**完全不問人**：

1. **CONFIRM fail-open（主因）**：`tool_executor.py` 對「參數無 `command`」的工具把
   政策 CONFIRM「log 一行 executed anyway」後照常執行——Manual 下 memory／檔案動詞／
   send_message／dispatch／skill/MCP 全家零審批。
2. **終端審批無處投遞**：審批通道為 `/terminal/stream` SSE（等「ToukaUI」訂閱者），
   ToukaChat 從未訂閱 → `_is_touka_ui_available()` 恆 False → `execute_command` 自動
   `permission_denied/ui_unavailable`，全程不問人（skill 安裝審批同通道＝agent 發起
   必空等 180s 後被拒）。
3. **auth_level 從未持久化**：前端 AuthSwitcher 的選擇送 `POST /v1/ui/sessions/{id}`
   被 `SessionModel`（無此欄位）靜默丟棄；DB `user_sessions.auth_level` 永遠停在預設
   `'Manual'`；`permissions.json` 的 `default_auth_level` 為執行期零讀取的死設定，
   ToukaChat RbacMatrix 還有一顆編輯它的無作用旋鈕。

引擎本身（`safety.py` 四級矩陣＋黑名單先行＋ADR-0089 唯讀豁免）語意正確——本案修
其輸入（分類、持久化）與輸出（CONFIRM 處置、審批通道），不重寫引擎。

## 決策（五個決策點；維護者核准，第 3 點為維護者修改版）

1. **工具風險分類 SSOT**：`auth_policies.json` 新 `tool_risk`——三類
   （read_only／safe／dangerous）＋聚合多動詞工具按 `action_param` 逐動詞細分；
   MCP／runtime 未列名一律 default dangerous（fail-safe）；40 原生工具全分類，
   Guard #32 擴充＝打錯字防護＋parity 棘輪（新工具未分類＝CI 紅）。
   `read_only_tools` 欄位汰換（ADR-0089 語意由 read_only 類承接：全等級 ALLOW 免 HITL）。
2. **決策表**（黑名單恆 DENY 凌駕一切，含 FullYOLO）：

   | | read_only | safe | dangerous |
   |---|---|---|---|
   | ReadOnly | ALLOW | DENY | DENY |
   | Manual | ALLOW | CONFIRM | CONFIRM |
   | SafeAuto | ALLOW | ALLOW | CONFIRM |
   | FullYOLO | ALLOW | ALLOW | ALLOW |

   終端指令沿 regex 白名單分類（命中＝safe、其餘＝dangerous）。**刻意變更**：
   ReadOnly 下白名單指令由舊 ALLOW 改 DENY（pytest/npm test 會執行程式碼）。
3. **無人 session 設定時定權**（維護者修改版）：`/heartbeat/awaken` 新明確四級
   `auth_level` 欄位（優先於 safe/full 舊映射；不回寫 session row——home 模式共用
   人類家會話，不得踩掉 AuthSwitcher）；子代理組裝／執行兩階段統一**繼承父 ctx**
   （派工當下人類的選擇＝事先決定，子代理不可自升）。headless 的 CONFIRM 解算
   ＝fail-closed 拒絕。
4. **fail-closed 於 H1 即翻**：CONFIRM 未獲人類決定前**工具函式零呼叫**（防退化錨
   `test_hitl_fail_closed`）；過渡期 Manual＝非唯讀全拒（誠實），H2/H3 補審批通道。
5. **死設定拆除**：`default_auth_level` 全面移除（後端模型/config/.example＋前端
   RbacMatrix 旋鈕/types/i18n/測試）。

## H1 成果（語意層）

`tool_risk` 分類 SSOT＋`safety.py` 按分類求值＋executor fail-closed＋動詞抽取進審計
（`environment["tool_action"]`，不記整包 args）＋`SessionModel.auth_level` 持久化
（upsert 本就支援、路由補傳；前端水合鏈本就完整＝零前端改動閉環）＋heartbeat 明確
四級欄位＋子代理兩階段一致＋`/debug/execute` 尊重 `payload.auth_level`（原硬編
Manual 且註解謊稱「最大權限」）＋測試沙箱播種生產分類表（fail-open 曾掩蓋空 config
吃 DEFAULT 全 dangerous）。pytest 3303／vitest 983／guard 41/41。

## H2 成果（統一審批 broker）

`services/approval_service.py`（register→wait〔Gate-2 阻塞〕→resolve〔人類端點〕；
180s 逾時自動拒、單發解算、cap 50、AUDIT log）＋審批事件走**聊天 SSE 本身**
（`tool_approval_request`／`tool_approval_resolved`，builder 進 `tool_events` 封套
SSOT、Guard #38 擴充納管）＋`POST /v1/tools/approve|reject/{exec_id}`（**is_human
硬性閘**＝agent token 含 Owner 一律 403 杜絕自批；admin／`owned_agents`／session
`owner_user_id` 三路放行；Origin CSRF）＋四家族收斂（互動會話 CONFIRM：approved 才
執行；headless 維持 H1 錨）＋終端收斂（互動走 broker＋`_hitl_preapproved` 跳過
terminal 自帶審批；headless 保留遺留通道）＋`request_approval` 增 `emit`（skill
安裝／創建審批請求同步進聊天流；無任何通道快速拒絕、不再空等 3×60s）。互動判準
＝`ctx.resumable_stream`（ask_user G10 同款）。pytest 3332／guard 41/41。

## H3 成果（ToukaChat 審批卡）

connections.ts 白名單（live＋resume 共用 dispatchSseLine＝重播一致）＋
`applyEventToTree`（審批以 call_id 建 `awaiting_approval` 步驟、核准後 tool_start
**就地升級**不重複建行、resolved 記 decision、拒絕／逾時復用 P3 permission_denied
黃牌收斂）＋`ToolApprovalCard`（參數預覽／風險章／倒數／批准拒絕；404/403 還原
按鈕）＋tracker `awaiting` 黃態＋並行多筆獨立＋i18n 兩語系。**設計決定**：skill
安裝審批仍解算於 `/terminal/approve`（Event 在 terminal 遺留 registry；useEvolution
不遷移）。vitest 996／guard 41/41。

## H4 成果（文件與提示對齊）

`<validation_protocol>` Manual 句依審批通道分寫（互動＝「pauses for a human
approval card」、headless＝「denied fail-closed」——舊句「each tool call is
human-approved」在 fail-open 年代是謊言）＋`<harness_context>` 審批承諾句換新並
**加 Guard #40 新釘**（"pause for human approval" ⇄ `_await_human_approval`）＋
AuthSwitcher 四級 desc 對齊決策表＋SECURITY.md HITL 節＋本 ADR。

## 後果

- **正面**：HITL 四級對終端／原生／memory／MCP 全家族真實生效；Manual 全鏈路可用
  （審批卡在對話裡）；agent 不可自批；審批歷史隨 session 持久化；skill 安裝審批
  在 ToukaChat 復活；提示文字與行為對齊。
- **代價／限制**：審批狀態行程內（重啟即清，與 terminal 同語意）；headless Manual
  ＝實質唯讀（設計如此——要自動化選 SafeAuto/FullYOLO 於設定時）；`/terminal/*`
  審批端點與遺留 ToukaUI 通道保留（skill 安裝＋外部生態），未來可另案收斂。
- **既有部署遷移**：`auth_policies.json` 需更新為 `tool_risk` 結構（缺 tool_risk
  區塊＝Guard #32 FAIL；執行期缺檔 fallback＝全 dangerous fail-safe）。

## 相關

- [提案：HITL 全面修復＋思考摺疊](../proposals/hitl-unification-and-reasoning-fold.md)
- [ADR-0089](0089-read-only-tool-auto-allow.md)（read_only 豁免——欄位由 tool_risk 承接）
- [ADR-0123](0123-verification-convention.md)（validation_protocol；本案對齊其 Manual 句）
- [ADR-0139](0139-harness-context-block.md)（harness_context；本案更新審批承諾句＋Guard #40 新釘）
- diag 記錄：Sumi/grok-4.5 三症狀（2026-07-11；當時記在 HANDOVER，現在只留在開源前的私人歷史）
