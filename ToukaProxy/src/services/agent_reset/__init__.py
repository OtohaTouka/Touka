"""DebugAgent 破壞性重置（軌B Batch 6）。

重置＝清「累積資料」、**保留身分/註冊**；硬鎖只允許 DebugAgent、絕不誤刪真實 agent；
清空前先全系統備份（備份失敗即中止）。詳見 `.claude/plans/scorecard-batch6-reset.md`。
"""
