"""Batch 5+6 P1-B SNS bots pure-function tests (TDD).

Covers the bots-router helpers that implement:
  - S1: token write-only masking (GET masks, POST preserves MASK by id)
  - B2: stable bot id (assigned when missing)
  - B3: agent_id flattened to a string at the API boundary
All logic lives in pure functions so it is testable without HTTP/singletons.
"""

from __future__ import annotations

from src.routers.bots import (
    _flatten_agent_id,
    _mask_tokens,
    _normalize_config,
    _preserve_tokens,
)
from src.utils.secrets import MASK


# ── B3: agent_id flattening ────────────────────────────────────────────────

def test_flatten_agent_id_from_legacy_object():
    assert _flatten_agent_id({"agent_id": "Aria", "role": "Base"}) == "Aria"


def test_flatten_agent_id_passthrough_string():
    assert _flatten_agent_id("Aria") == "Aria"


def test_flatten_agent_id_empty_and_none():
    assert _flatten_agent_id(None) == ""
    assert _flatten_agent_id("") == ""
    assert _flatten_agent_id({}) == ""


# ── S1: GET masking ────────────────────────────────────────────────────────

def test_mask_tokens_masks_nonempty_keeps_empty():
    cfg = {
        "discord": [{"id": "d1", "token": "super-secret", "agent_id": "A", "enabled": True}],
        "telegram": [{"id": "t1", "token": "", "agent_id": "B", "enabled": False}],
    }
    out = _mask_tokens(cfg)
    assert out["discord"][0]["token"] == MASK
    assert out["telegram"][0]["token"] == ""  # empty stays empty (前端據此判斷未設定)
    # non-token fields untouched
    assert out["discord"][0]["agent_id"] == "A"
    assert out["discord"][0]["id"] == "d1"


# ── B2: stable id + normalization ──────────────────────────────────────────

def test_normalize_assigns_id_when_missing():
    cfg = {"discord": [{"token": "x", "agent_id": "A", "enabled": True}], "telegram": []}
    out, changed = _normalize_config(cfg)
    assert changed is True
    assert out["discord"][0]["id"]  # non-empty
    assert len(out["discord"][0]["id"]) == 32  # uuid4().hex


def test_normalize_flattens_legacy_object_agent_id():
    cfg = {
        "discord": [{"id": "d1", "token": "x", "agent_id": {"agent_id": "Aria"}, "enabled": True}],
        "telegram": [],
    }
    out, changed = _normalize_config(cfg)
    assert changed is True
    assert out["discord"][0]["agent_id"] == "Aria"


def test_normalize_idempotent_when_already_clean():
    cfg = {
        "discord": [{"id": "d1", "token": "x", "agent_id": "A", "enabled": True,
                     "session_mode": "independent", "is_multi_agent": False}],
        "telegram": [],
    }
    out, changed = _normalize_config(cfg)
    assert changed is False
    assert out["discord"][0] == cfg["discord"][0]


# ── S1: POST write-only preservation ───────────────────────────────────────

def test_preserve_restores_masked_token_by_id():
    existing = {"discord": [{"id": "d1", "token": "real-token", "agent_id": "A", "enabled": True}],
                "telegram": []}
    incoming = {"discord": [{"id": "d1", "token": MASK, "agent_id": "A", "enabled": False}],
                "telegram": []}
    out = _preserve_tokens(incoming, existing)
    assert out["discord"][0]["token"] == "real-token"  # restored
    assert out["discord"][0]["enabled"] is False        # other edits applied


def test_preserve_keeps_new_real_token():
    existing = {"discord": [{"id": "d1", "token": "old", "agent_id": "A", "enabled": True}],
                "telegram": []}
    incoming = {"discord": [{"id": "d1", "token": "new-token", "agent_id": "A", "enabled": True}],
                "telegram": []}
    out = _preserve_tokens(incoming, existing)
    assert out["discord"][0]["token"] == "new-token"  # changed token persists


def test_preserve_new_bot_with_real_token_kept():
    existing = {"discord": [], "telegram": []}
    incoming = {"discord": [{"id": "new1", "token": "fresh", "agent_id": "A", "enabled": True}],
                "telegram": []}
    out = _preserve_tokens(incoming, existing)
    assert out["discord"][0]["token"] == "fresh"


def test_preserve_masked_without_match_becomes_empty():
    """Defensive: a MASK token on a bot that HAD an id but matches nothing → empty."""
    existing = {"discord": [], "telegram": []}
    incoming = {"discord": [{"id": "ghost", "token": MASK, "agent_id": "A", "enabled": True}],
                "telegram": []}
    out = _preserve_tokens(incoming, existing)
    assert out["discord"][0]["token"] == ""
    assert out["discord"][0]["token"] != MASK


def test_preserve_premigration_no_id_recovers_by_position():
    """R2/DLC-1: a pre-migration bot WITHOUT an id sends MASK before GET backfills ids.
    Both sides lack stable ids → recover the real token positionally (not lost)."""
    existing = {"discord": [{"token": "real-token", "agent_id": "A", "enabled": True}],
                "telegram": []}
    incoming = {"discord": [{"token": MASK, "agent_id": "A", "enabled": False}],  # no id
                "telegram": []}
    out = _preserve_tokens(incoming, existing)
    assert out["discord"][0]["token"] == "real-token"  # recovered positionally, NOT lost
    assert out["discord"][0]["id"]  # a stable id is now assigned


def test_preserve_premigration_no_id_out_of_range_becomes_empty():
    """A no-id MASK bot beyond the existing list length has nothing to recover → empty."""
    existing = {"discord": [], "telegram": []}
    incoming = {"discord": [{"token": MASK, "agent_id": "A", "enabled": True}],  # no id, no existing
                "telegram": []}
    out = _preserve_tokens(incoming, existing)
    assert out["discord"][0]["token"] == ""
