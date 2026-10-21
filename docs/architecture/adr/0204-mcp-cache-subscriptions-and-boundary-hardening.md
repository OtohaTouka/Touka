# ADR-0204：MCP cache、subscriptions 與邊界強化

- **狀態**：Accepted
- **日期**：2026-07-31
- **相關**：[ADR-0203](0203-mcp-feature-families-and-schema-boundary.md)、
  [MCP 2026 完整遷移計畫](../proposals/mcp-2026-07-28-protocol-migration.md)

## 脈絡

P5 已建立 Tools／Resources／Prompts canonical data plane，但尚未統一 freshness、
subscription、per-request header、icon 與 telemetry 邊界。直接依賴 SDK 預設 cache
或把通知、header、外部 icon 與 trace 原樣投影，會造成跨 principal 快取、過期資料
被視為新鮮、秘密進 header／log，以及 icon SSRF。

## 決策

1. 每個 installation 只有一個 SDK backing store。public cache 仍綁 installation；
   private cache 再綁 principal，TTL 與 scope hints 由 store 執法。可關閉 cache 並退回
   每次讀取。
2. subscription supervisor 綁完整 handle key（installation、principal、protocol、
   generation），限制 filter、重連退避與資源訂閱集合；掉線後標 stale，通知只做
   invalidation，不混入 request-scoped progress/log。
3. `x-mcp-header` 只接受經 schema 驗證的 primitive／visible-ASCII／大小有限欄位；
   secret/PII 預設拒絕，需 installation 級逐 tool 核准。投影 header 不參與 RBAC，
   且 log／trace 一律遮罩。
4. icon 只接受 HTTPS 或受限 data URI；遠端抓取禁止 credential、redirect 與環境
   proxy，並驗 host、MIME magic、bytes、維度與動畫；SVG 拒絕。
5. OTel 僅輸出 allowlist metadata；raw args、header、token、resource、prompt、
   query、exception、tracestate 與 baggage 不輸出，exporter 預設關閉。

## 結果與邊界

- P6 runtime 回歸涵蓋 cache partition/TTL、stale listener、header DoS/secret、
  icon SSRF/MIME 與 telemetry payload。
- P7 為了在 retry 前重新授權，modern 三種互動操作使用低階 session driver；因此
  互動 operation 不依賴 SDK 高階 response cache，catalog cache 與 subscription
  invalidation 仍維持 P6 邊界。
- 未修改、掛載或遷移主機 `data/`。
