# ADR-0188: MCP enabled 停用開關咽喉執法（runtime registry 排除）

## 狀態

✅ 已接受（MCP connectors 調查 F4，維護者點名修復；接續 ADR-0186／0187）

## 日期

2026-07-25

## 背景

F4＝`enabled` 停用開關「半承接」。前端有完整 toggle 與灰色「已停用」顯示（`McpServerItem.tsx`）、`POST /mcp/ui/config` 忠實落盤 `enabled: false`；但後端唯一消費者只有 dispatcher 技能清單（`skill_service.py` 的 `cfg.get("enabled", True)`）。`ProtocolServerConfig` **沒有 `enabled` 欄位**——pydantic `extra='ignore'` 把它靜默吞掉——導致：

1. `initialize_all` 照樣 **spawn 停用者的子進程**；
2. 非 dispatcher 注入路徑（`tool.py` else 分支）其工具照樣廣告給模型；
3. `ensure_connected`（動態掛載）照樣可連。

UI 語意與 runtime 行為不符＝fails open。

## 決策與實作

**單一執法點＝`ProtocolRegistry.load_config`**（咽喉模式，比照 E66 前例）：

- `ProtocolServerConfig` 增 `enabled: bool = True`（缺席＝預設啟用，既有設定零遷移）。
- `load_config` 對 `enabled: false` 條目**不載入 runtime registry**（info log 有聲）。

下游全部自然斷路、零多點執法：`initialize_all` 迭代 registry → 不 spawn；`ensure_connected`（`connect_mcp_server` 工具與技能 `.mcp.json` 接線的共同入口）查無 → `False` fail-closed（沿用既有 warning）；`resolve_tool` → `None` ＝派發斷路；`get_active_server_info` 不列（前端「已停用」狀態本就由 config 端 client-side 判定、restart 鈕本已 disabled＝**前端零改動**）。

**熱重載語意升級**：設定頁 toggle → save → `registry.load_config()` + `initialize_all()`（先 `cleanup()` 關全部、再連啟用者）＝**停用即殺進程、再啟用即重生**——toggle 從「只影響 dispatcher 文案」變成真開關。

附帶語意（記錄）：`validate_skill_mcp_wiring` 對「技能需要但被停用」的 server 照樣告警（訊息以「未註冊」表述，語意正確——技能確實接不上）；permissions／scorecard 的 registry 列表同步排除停用者。

### 變異驗證（逐條隔離、皆已還原）

| 變異 | 結果 |
|------|------|
| M1 整刪過濾塊 | 4 紅（registry 排除／toggle 重載／initialize_all 零 spawn／ensure_connected fail-closed 全滅）；dispatcher pin 綠 |
| M2 條件極性反轉（`if parsed.enabled: continue`） | 同 4 紅（反方向：啟用者消失、停用者載入）；dispatcher pin 綠 |
| baseline | `test_mcp_enabled_gate.py` 5 passed＋MCP 鄰域六檔 57 passed |

## 測試

`tests/unit/test_mcp_enabled_gate.py` 新檔 +5：registry 排除（含缺席預設啟用）／toggle 後重載回歸／**行為級** `initialize_all` 零 spawn（真 ProtocolManager＋捕 connect）／`ensure_connected` fail-closed／dispatcher `<mcp_registry>` 清單排除 pin（既有生產行為首次釘測）。

TDD：4 紅＋1 pin 綠先行 → 實作轉綠 → 兩變異互證極性。

## 後果

- 停用開關語意誠實：UI 顯示＝runtime 事實；停用者不再消耗子進程。
- F5（種子單薄）／F6（前端安裝徽章鍵不匹配）不在本批——由維護者點名。

## 教訓

pydantic `extra='ignore'` 會把未宣告欄位靜默吞掉——「模型沒這欄」＝「設定寫了也沒人讀」的無聲失效型態；與 F3 的 `response_model` 剝除（宣告面缺欄→回應面丟失）是同一家族的兩個方向。跨層欄位要嘛兩端都宣告、要嘛 pin 測試釘住。

## 關聯

- 前作：ADR-0186／0187（同一 MCP connectors 調查源）
- 既有行為：ADR-0161 P4-2（dispatcher 清單 `enabled` 過濾＝本批 pin 對象）
- 模式前例：E66 咽喉執法（ADR-0176）
- 台帳：memory `touka-mcp-hub-review`（F1–F6）
