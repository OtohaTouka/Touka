# IDE交班

範圍：`ToukaIDE/**`：VS Code 擴充。

這份只放這個範圍沒做完、有未解限制、做完還沒部署或要特別操作的事。總覽見 [HANDOVER](../../HANDOVER.md)，舊內容見 [封存](../changelog/handover-archive-2026-11.md)。做完一項就從這裡刪掉，紀錄寫進 [CHANGELOG](../CHANGELOG.md)。

## 現況

- **IDE 讀寫檔不跟隨連結、連線資訊檔只給自己（2026-10-30，ADR-0294，PR B）**：ToukaIDE 的路徑檢查不再放過懸空連結，讀寫一律經 `noFollowFile.ts`（FIFO 不卡住，Linux 開檔後核對實際位置）；差異預覽按「接受」時重新檢查、路徑變了就不寫，暫存檔放在私人資料夾。Linux／macOS 上 IDE 只在自己的 0700 資料夾寫 0600 的連線資訊檔，上層暫存資料夾不安全就不寫並跳警告；Touka 只讀自己的、別人不能寫的資料夾與檔案，進不去的資料夾略過。Windows 照舊；Docker 部署本來就看不到 IDE 的連線資訊檔。POSIX 的行為由 Linux CI 的 `toukaide`、`contracts` 工作驗證。

## 也和這裡有關

- 三種模式、底線與審查員上線；繞過與準確率未解：見 [proxy.md](proxy.md)。
- 主要模型分兩欄、模型名不分大小寫；現有值要自己確認：見 [proxy.md](proxy.md)。
- 演化先問人、MCP 不再拿金鑰；升級要檢查 MCP 連線：見 [isolation.md](isolation.md)。
- 5.5 小工作少想、價目修正；審查員等待要手動改成 60：見 [proxy.md](proxy.md)。

## 待排小項

目前沒有。
