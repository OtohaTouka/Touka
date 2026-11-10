# 本機小幫手交班

範圍：`ToukaLocal/**`：使用者電腦上的小幫手（ADR-0312）。

這份只放這個範圍沒做完、有未解限制、做完還沒部署或要特別操作的事。總覽見 [HANDOVER](../../HANDOVER.md)。

## 現況

- 第一版試行中，只有 Windows。協議改版時，小幫手要重新安裝（`ToukaLocal/README.md`）。
- `ToukaLocal/` 不走 Touka 的 SSOT，各條禁令怎麼適用見 ADR-0312 第 11 點。
- 連結和真檔案只有在 Windows 上才測得到，CI 只有 Linux：改 `ToukaLocal/` 時要在本機跑 `tests/unit/local_helper/`。
- 自動測試沒有真的把檔案丟進回收桶，只在 2026-11-10 人工試過兩次，第二次比對了帶與不帶 `FOF_NO_CONNECTED_ELEMENTS`：帶了只丟清單上的 `a.html`，`a_files` 留著。改 `winapi.RecycleBin` 時要用可拋棄的檔案再試。
- 視窗還沒有人實際操作過：顯示邏輯和流程有測試，真的按核准、拒絕、關視窗要等第一次試行。
- OneDrive 資料夾本身會不會帶雲端佔位屬性沒有實測。如果會，整個 OneDrive 底下都會當連結拒絕，試行時留意。

## 也和這裡有關

- Touka 端的端點、工具和配對：見 [proxy.md](proxy.md)。

## 待排小項

- GHCR 舊安裝升級後，新錯誤碼不在播種的 `errors.json` 裡（ADR-0140 ⑥：升級不刷新播種內容），小幫手的失敗路徑會變成 500。只影響這個新功能，和既有的同類問題一起處理。
- 小幫手不驗證對方是不是真的 Touka（ADR-0312「後果」），試行後決定要不要加亂數挑戰。
- Python 裝在 Touka 資料夾的上層，又用預設的 `./data` 掛載時，小幫手會判成程式露出而停止服務。方向是安全的，配置也少見；換一個位置的 Python 就能解。
