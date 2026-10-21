"""Tool-call eval harness scaffold.

Measures how reliably agents select and call Touka's native tools:
selection accuracy, wrong-tool / invalid-param / hallucinated-tool / tool-error
rates, and policy-denial accuracy.

Runs OFFLINE (replay) by default so it is CI-safe with no API keys. LIVE mode
（ADR-0136 已接通）跑真實 in-process episode——完整 pipeline＋工具真的執行，
需顯式 ``live_model``（擁有者手動帶金鑰；見 ``live_episode.py`` 與 ``README.md``）。
另含互審 gate 驗收軌：注錯欄位＋gate 指標（``metrics``）＋非 LLM oracle（``oracle``）。
"""
