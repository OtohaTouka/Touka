"""Live smoke 共用基礎（ADR-0103）。

提供 sandbox 隔離 helper：standalone smoke 用 ``with TestClient(app)`` 觸發完整 lifespan
時，須先以 sandbox env 啟動（空 config / 無 bots / 無動態發現），避免真實重量級啟動殘留
背景任務與執行緒，導致 Windows ProactorEventLoop 在 teardown 死鎖。
"""
