"""Unit tests for N12 security fix (TDD).

N12 — Dynamic SQL placeholder building: get_home_session_id used an f-string
      to dynamically construct the IN clause. After fix, the query must be a
      fixed literal with exactly 2 placeholders — no f-string SQL assembly.
"""

from __future__ import annotations

import inspect

from src.utils import async_db


def test_get_home_session_id_uses_fixed_query():
    """get_home_session_id source must NOT build the IN clause dynamically."""
    src = inspect.getsource(async_db.SessionService.read_home_snapshot)

    # After N12 fix: the placeholder string must be a literal, not assembled
    # by 'placeholders = ",".join(["?"] * len(...))'
    assert 'placeholders = ' not in src, (
        'N12 fix: SQL placeholder must be a fixed literal, not dynamically generated'
    )
    assert '"IN ({placeholders})"' not in src and \
           "f'IN ({placeholders})'" not in src, (
        'N12 fix: f-string IN clause with dynamic placeholders must be removed'
    )


def test_get_home_session_id_query_is_fixed_two_placeholders():
    """The fixed IN clause must have exactly 2 question marks."""
    src = inspect.getsource(async_db.SessionService.read_home_snapshot)
    # The fixed IN clause should contain "IN (?, ?)" or "home_for_agent IN (?, ?)"
    assert 'IN (?, ?)' in src, (
        'N12 fix: must use fixed IN (?, ?) with exactly 2 placeholders'
    )
