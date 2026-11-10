# 本機小幫手交班

範圍：`ToukaLocal/**`：使用者電腦上的小幫手（ADR-0311）。

這份只放這個範圍沒做完、有未解限制、做完還沒部署或要特別操作的事。總覽見 [HANDOVER](../../HANDOVER.md)。

## 現況

- 第一版試行中，只有 Windows。協議改版時，小幫手要重新安裝（`ToukaLocal/README.md`）。
- 真回收桶和連結只有在 Windows 上才測得到，CI 只有 Linux：改 `ToukaLocal/` 時要在本機跑 `tests/unit/local_helper/`。

## 也和這裡有關

- Touka 端的端點、工具和配對：見 [proxy.md](proxy.md)。

## 待排小項

目前沒有。
