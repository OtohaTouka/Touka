"""對抗式互審子系統（in-process 提案 Phase 3，ADR-0137）——證據接地紅隊互審.

verdict 的權威來源不是模型意見，而是 critic 無法以幻覺竄改的可執行證據：
``evidence.EvidenceCollector``（純 Python）產 ``EvidenceBundle`` →
``reviewer.GroundedReviewer``（fail-closed）出 ``CritiqueReport`` →
``review_loop`` 驅動往返 FSM ＋ 隔離／預算硬界 → 結論經
``MissionManager.record_review`` 落看板（gate＝落盤不變式）。
設計全文見 docs/architecture/proposals/adversarial-review-subsystem-design.md。
"""
