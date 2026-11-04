# Heartbeat交班

範圍：`ToukaHeartbeat/**`：排程與喚醒。

這份只放這個範圍沒做完、有未解限制、做完還沒部署或要特別操作的事。總覽見 [HANDOVER](../../HANDOVER.md)，舊內容見 [封存](../changelog/handover-archive-2026-11.md)。做完一項就從這裡刪掉，紀錄寫進 [CHANGELOG](../CHANGELOG.md)。

## 現況

- **心跳、排程與 SNS 對話歸屬已接線（2026-10-17，ADR-0276）**：執行歷史與所有內容寫入核對精確靈魂，家定位／注入拒絕矛盾或多家；scope 錯誤為 403 `SESSION_SCOPE_MISMATCH`（條件保存沿 typed failed）。心跳／SNS 改每靈魂頻道，舊共享 ID 保留、不搬、不續寫；SNS 跨目標限同一唯一人類擁有者，群聊不寫單靈魂 session。排程設定不預檢 session，執行時拒絕。
  - 人類 UI 可在兩側授權成立時換綁非家對話，非空換綁的 `session_scope_version` 永久降為 0；嚴格空草稿只保留原值。家欄只經 set_home，須先取消家再換靈魂；一般 save 忽略家欄。生成失敗／取消／no_text 不回滾已提交換綁。
  - 舊列 marker=0、不回填；受管 INSERT 才設 1，公開 API 不回傳。部署後舊 revision 失效，須重讀 snapshot；所有 writer 須一起升級、不混跑。搜尋已由 ADR-0277 接上，舊列與非空換綁列不列入；並發首次建家仍可能產生多家、後續拒絕。五個新安全檔共 97 案，測試只用 sandbox／mock；尚未部署。

## 也和這裡有關

- 三種模式、底線與審查員上線；繞過與準確率未解：見 [proxy.md](proxy.md)。
- 主要模型分兩欄、模型名不分大小寫；現有值要自己確認：見 [proxy.md](proxy.md)。
- 本機測試前置、建置產物宣告、擋公開金鑰；升級要 compose up：見 [proxy.md](proxy.md)。
- 新增可信活動時間軸，心跳也注入；關機時事件可能丟：見 [memory.md](memory.md)。

## 待排小項

- Heartbeat 自己的 8082 排程 API 在容器重建前仍接受原本的金鑰值（只綁本機；執行一次 `docker compose up -d` 後關上，ADR-0251）。
- 心跳派出的子代理不在破壞性操作的登記裡：脈衝結束後子代理繼續跑，刪除與重置看不到它；結束時把結果寫進父靈魂的個人空間，刪除之後才寫會重建那個資料夾。候選：把子代理也登記、刪除時一併算（ADR-0287）。
