"""Phase 3 步驟 6（互審設計文件 §4.6）：預算四道硬界——TDD.

- **往返上限**：``GLOBAL_REVIEW_CAP=3``（決策 1）為 SSOT 天花板；config **只能收緊
  不能放寬**（``_clamp_quota`` 慣例）；
- **深度**：兩層行為承諾——主 agent context（depth 0）可開子 loop，子（>=1）不可再開；
- **spawn 記帳**：mission 級 ledger＋單一 lock 防 lost-update（並發 register 不超帳）；
- **成本閘**：互審路徑 cap 有數字即強制查（**無視 cap.enabled**——互審成本閘不可關）。
"""

from __future__ import annotations

import asyncio

import pytest
from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.review.budget import (
    GLOBAL_REVIEW_CAP,
    MAX_REVIEW_DEPTH,
    _reset_review_ledger,
    cost_ceiling_exceeded,
    depth_allows_spawn,
    effective_review_cap,
    get_review_ledger,
)


@pytest.fixture(autouse=True)
def _fresh_ledger():
    _reset_review_ledger()
    yield
    _reset_review_ledger()


# --------------------------------------------------------------------------- #
# 1. CAP clamp：只能收緊不能放寬
# --------------------------------------------------------------------------- #


def test_global_cap_is_three():
    assert GLOBAL_REVIEW_CAP == 3  # 決策 1：放寬＝修天花板＝一次人審 commit


def test_effective_cap_clamps_only_tighter():
    assert effective_review_cap(None) == GLOBAL_REVIEW_CAP
    assert effective_review_cap(99) == GLOBAL_REVIEW_CAP  # 不可放寬
    assert effective_review_cap(2) == 2  # 可收緊
    assert effective_review_cap(0) == 1  # 下限 1（0＝關 gate，不在此參數的語義內）


# --------------------------------------------------------------------------- #
# 2. 深度：兩層承諾
# --------------------------------------------------------------------------- #


def test_depth_two_layers():
    assert MAX_REVIEW_DEPTH == 1
    assert depth_allows_spawn(0) is True  # 主 agent context 可開子
    assert depth_allows_spawn(1) is False  # 子不可再開孫
    assert depth_allows_spawn(2) is False


# --------------------------------------------------------------------------- #
# 3. mission 級 spawn ledger
# --------------------------------------------------------------------------- #


async def test_ledger_counts_and_ceiling():
    ledger = get_review_ledger()
    assert await ledger.register_spawn("m1", ceiling=2) is True
    assert await ledger.register_spawn("m1", ceiling=2) is True
    assert await ledger.register_spawn("m1", ceiling=2) is False  # 超帳拒開
    assert ledger.spawned("m1") == 2


async def test_ledger_isolated_per_mission():
    ledger = get_review_ledger()
    await ledger.register_spawn("m-a", ceiling=1)
    assert await ledger.register_spawn("m-b", ceiling=1) is True
    assert ledger.spawned("m-a") == 1
    assert ledger.spawned("m-b") == 1


async def test_ledger_concurrent_no_lost_update():
    """並發 register 不超帳（mission 級 lock；重演 per-agent 鎖 lost-update 的反例）。"""
    ledger = get_review_ledger()
    results = await asyncio.gather(
        *[ledger.register_spawn("m-conc", ceiling=5) for _ in range(10)]
    )
    assert sum(1 for r in results if r) == 5
    assert ledger.spawned("m-conc") == 5


# --------------------------------------------------------------------------- #
# 4. 成本閘：互審路徑不可關
# --------------------------------------------------------------------------- #


def test_cost_ceiling_forced_even_when_cap_disabled(monkeypatch):
    identity = AgentIdentity.for_role(agent_id="BudgetReview", role="Owner")
    monkeypatch.setattr(autonomy_service, "get_total_cost_usd", lambda _i: 12.0)

    # cap.enabled=False 也強制查（互審成本閘不可關）——與 is_over_budget fail-open 相反
    config = {"cap": {"enabled": False, "max_total_cost_usd": 10.0}}
    assert cost_ceiling_exceeded(identity, config=config) is True

    config_under = {"cap": {"enabled": False, "max_total_cost_usd": 20.0}}
    assert cost_ceiling_exceeded(identity, config=config_under) is False


def test_cost_ceiling_without_number_cannot_block(monkeypatch):
    """無數字＝無法強制天花板（誠實：不擋、不假裝有界）。"""
    identity = AgentIdentity.for_role(agent_id="BudgetNoCap", role="Owner")
    monkeypatch.setattr(autonomy_service, "get_total_cost_usd", lambda _i: 999.0)
    assert cost_ceiling_exceeded(identity, config={"cap": {}}) is False
    assert cost_ceiling_exceeded(identity, config={}) is False
