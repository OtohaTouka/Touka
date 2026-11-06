"""Pinning tests for ``src.utils.custom_io`` (CustomModelIO physical-file guard).

These functions were *covered-but-zero-asserted* by the existing IDOR/sandbox
suite (which only asserted the security-rejection paths). This file pins the
HAPPY paths and exact return STRUCTURES so a one-line logic change in the
production source flips a result.

All filesystem access is redirected to ``tmp_path`` via monkeypatch of
``get_custom_dir`` / ``_get_base_dir``, or the ``zone_data_root`` fixture for the delete
and move paths that go through ResourceManager (ADR-0293) — the real ``data/`` dir is
NEVER touched.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.services import destructive_ops
from src.utils import custom_io

from tests.support.zone_records import expect_zone_refusal

IDENT = AgentIdentity.for_role(agent_id="agentX", role="Base")


@contextmanager
def _held(identity):
    """刪除路由的 ``destructive_op`` 會做的標記（R11）."""
    destructive_ops.hold(identity)
    try:
        yield
    finally:
        destructive_ops.release(identity)


# ===========================================================================
# delete_custom_dir
# ===========================================================================


class TestDeleteCustomDir:
    def test_returns_false_when_dir_missing(self, zone_data_root):
        (zone_data_root / "custom").mkdir()
        with _held(IDENT):
            assert custom_io.delete_custom_dir(IDENT) is False

    def test_returns_true_and_dir_gone_when_exists(self, zone_data_root):
        d = zone_data_root / "custom" / "agentX"
        d.mkdir(parents=True)
        (d / "weights.json").write_text("{}", encoding="utf-8")

        with _held(IDENT):
            assert custom_io.delete_custom_dir(IDENT) is True
        assert not d.exists()

    def test_refuses_unless_a_destructive_op_holds_the_model(self, zone_data_root):
        """只有走 R11 的刪除（停指令、拒絕連結）能刪；別的入口直接呼叫就丟例外."""
        d = zone_data_root / "custom" / "agentX"
        d.mkdir(parents=True)
        with pytest.raises(ToukaException):
            custom_io.delete_custom_dir(IDENT)
        assert d.exists()

    def test_returns_false_when_the_id_is_not_one_segment(self, zone_data_root):
        (zone_data_root / "custom" / "agentX").mkdir(parents=True)
        escaping = AgentIdentity(agent_id="../custom")
        with expect_zone_refusal("ZoneSegmentError"), _held(escaping):
            assert custom_io.delete_custom_dir(escaping) is False
        assert (zone_data_root / "custom" / "agentX").exists()


# ===========================================================================
# list_custom_ids
# ===========================================================================


class TestListCustomIds:
    def test_returns_empty_when_base_missing(self, tmp_path, monkeypatch):
        base = tmp_path / "app" / "custom"  # never created
        monkeypatch.setattr("src.utils.custom_io._get_base_dir", lambda: base)
        assert custom_io.list_custom_ids() == []

    def test_returns_dir_names_only(self, tmp_path, monkeypatch):
        base = tmp_path / "app" / "custom"
        base.mkdir(parents=True)
        (base / "modelA").mkdir()
        (base / "modelB").mkdir()
        (base / "modelC").mkdir()
        # A plain file must be ignored (only dirs counted).
        (base / "stray.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr("src.utils.custom_io._get_base_dir", lambda: base)

        assert sorted(custom_io.list_custom_ids()) == ["modelA", "modelB", "modelC"]

    def test_ignores_plain_files_only_one_dir(self, tmp_path, monkeypatch):
        base = tmp_path / "app" / "custom"
        base.mkdir(parents=True)
        (base / "onlyModel").mkdir()
        (base / "a.json").write_text("{}", encoding="utf-8")
        (base / "b.bin").write_text("data", encoding="utf-8")
        monkeypatch.setattr("src.utils.custom_io._get_base_dir", lambda: base)

        assert custom_io.list_custom_ids() == ["onlyModel"]


# ===========================================================================
# write_custom_file / read_custom_file / custom_file_exists
# ===========================================================================


class TestWriteReadRoundtrip:
    @pytest.fixture
    def cdir(self, tmp_path, monkeypatch):
        d = tmp_path / "custom" / "modelX"
        # Intentionally NOT pre-created: write_custom_file must mkdir(parents=True).
        monkeypatch.setattr("src.utils.custom_io.get_custom_dir", lambda identity: d)
        return d

    def test_write_then_read_returns_exact_content(self, cdir):
        assert custom_io.write_custom_file(IDENT, "weights.json", "hello-world") is True
        assert custom_io.read_custom_file(IDENT, "weights.json") == "hello-world"

    def test_write_creates_dir_and_file_on_disk(self, cdir):
        assert not cdir.exists()
        assert custom_io.write_custom_file(IDENT, "cfg.txt", "DATA") is True
        assert (cdir / "cfg.txt").read_text(encoding="utf-8") == "DATA"

    def test_read_missing_file_returns_none(self, cdir):
        cdir.mkdir(parents=True)
        assert custom_io.read_custom_file(IDENT, "absent.json") is None

    def test_exists_true_after_write(self, cdir):
        assert custom_io.custom_file_exists(IDENT, "weights.json") is False
        assert custom_io.write_custom_file(IDENT, "weights.json", "{}") is True
        assert custom_io.custom_file_exists(IDENT, "weights.json") is True

    def test_exists_false_for_missing(self, cdir):
        cdir.mkdir(parents=True)
        assert custom_io.custom_file_exists(IDENT, "ghost.json") is False

    def test_write_returns_false_when_dir_none(self, monkeypatch):
        monkeypatch.setattr(
            "src.utils.custom_io.get_custom_dir", lambda identity: None
        )
        assert custom_io.write_custom_file(IDENT, "f.txt", "x") is False

    def test_read_returns_none_when_dir_none(self, monkeypatch):
        monkeypatch.setattr(
            "src.utils.custom_io.get_custom_dir", lambda identity: None
        )
        assert custom_io.read_custom_file(IDENT, "f.txt") is None

    def test_exists_returns_false_when_dir_none(self, monkeypatch):
        monkeypatch.setattr(
            "src.utils.custom_io.get_custom_dir", lambda identity: None
        )
        assert custom_io.custom_file_exists(IDENT, "f.txt") is False


# ===========================================================================
# move_custom_dir
# ===========================================================================


class TestMoveCustomDir:
    @pytest.fixture
    def base(self, zone_data_root):
        # Nested so an escaping target would still land inside tmp_path for assertions.
        b = zone_data_root / "custom"
        b.mkdir()
        old = b / "modelX"
        old.mkdir()
        (old / "config.json").write_text("{}", encoding="utf-8")
        return b

    def test_success_moves_dir(self, base):
        assert custom_io.move_custom_dir("modelX", "modelY") is True
        assert not (base / "modelX").exists()
        assert (base / "modelY" / "config.json").read_text(encoding="utf-8") == "{}"

    def test_returns_false_when_old_missing(self, base):
        assert custom_io.move_custom_dir("ghost", "modelY") is False
        assert not (base / "modelY").exists()

    def test_returns_false_when_new_exists(self, base):
        (base / "modelY").mkdir()
        assert custom_io.move_custom_dir("modelX", "modelY") is False
        # Source untouched — nothing was moved.
        assert (base / "modelX" / "config.json").exists()

    def test_sandbox_escape_new_id_traversal_blocked(self, base, tmp_path):
        with expect_zone_refusal("ZoneSegmentError"):
            assert custom_io.move_custom_dir("modelX", "../../escape") is False
        assert (base / "modelX" / "config.json").exists()
        assert not (tmp_path / "escape").exists()

    def test_sandbox_escape_old_id_traversal_blocked(self, base):
        with expect_zone_refusal("ZoneSegmentError"):
            assert custom_io.move_custom_dir("../../etc", "modelY") is False
        assert not (base / "modelY").exists()
