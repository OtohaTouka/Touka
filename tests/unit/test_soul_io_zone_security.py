"""靈魂檔的檔名只能是單層、不能落進代理人資料夾（ADR-0293 PR 2 Q10）.

``update_my_soul`` 的 ``filename`` 來自模型。自訂模型的靈魂根是 ``custom/<id>``，底下的 ``drive``
是代理人資料夾（區）；舊程式只解析檢查目標本身，``drive/x`` 的 ``.tmp``、``.bak`` 會跟隨那裡的連結。
兩道檢查各有只有它擋得到的輸入：``sub/x`` 不在區裡（只有單層檢查擋）；``drive`` 是單層、
但是區的根（只有區檢查擋）。資料都在 ``zone_data_root`` 底下，區的判斷用真的 RM 根。
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from src.models.identity import AgentIdentity
from src.utils import soul_io

from tests.support.links import LINK_FOLLOWED

if TYPE_CHECKING:
    from pathlib import Path

SOUL = AgentIdentity.for_role(agent_id="Alice", role="Owner")
MODEL = AgentIdentity.for_role(agent_id="M1", role="Base")


@pytest.fixture
def soul_base(zone_data_root, monkeypatch) -> Path:
    """靈魂 Alice 的靈魂根 ``soul/Alice``."""
    base = zone_data_root / "soul" / "Alice"
    base.mkdir(parents=True)
    monkeypatch.setattr(
        soul_io.resource_manager,
        "get_vault_path",
        lambda kind, _identity: str(base) if kind == "soul" else None,
    )
    return base


@pytest.fixture
def model_base(zone_data_root, monkeypatch) -> Path:
    """自訂模型 M1 的靈魂根 ``custom/M1``（底下的 ``drive`` 是代理人資料夾）."""
    base = zone_data_root / "custom" / "M1"
    (base / "drive").mkdir(parents=True)
    monkeypatch.setattr(
        soul_io.resource_manager,
        "get_vault_path",
        lambda kind, _identity: str(base) if kind == "custom_soul" else None,
    )
    return base


@pytest.mark.parametrize("name", ["sub/x", "sub\\x", ".", "..", ""])
def test_only_single_level_names_are_soul_files(soul_base, name):
    """25a：``sub/x`` 不在任何區裡，只有單層檢查擋得到."""
    (soul_base / "sub").mkdir()
    assert soul_io._safe_target(soul_base, name) is None


def test_writing_a_nested_soul_file_is_refused(soul_base):
    (soul_base / "sub").mkdir()
    assert soul_io.write_soul_file(SOUL, "sub/x", "content") is False
    assert not (soul_base / "sub" / "x").exists()


@pytest.mark.parametrize("name", ["drive", "DRIVE"] if sys.platform == "win32" else ["drive"])
def test_the_drive_of_a_custom_model_is_not_a_soul_file(model_base, name):
    """25b：``drive`` 是單層名稱，但它是代理人資料夾的根，只有區檢查擋得到."""
    assert soul_io._safe_target(model_base, name) is None
    assert soul_io.soul_file_exists(MODEL, name) is False


def test_files_inside_the_drive_are_not_soul_files(model_base):
    (model_base / "drive" / "notes.md").write_text("mine", encoding="utf-8")
    assert soul_io.read_soul_file(MODEL, "drive/notes.md") is None
    assert soul_io.delete_soul_file(MODEL, "drive/notes.md") is False
    assert (model_base / "drive" / "notes.md").read_text(encoding="utf-8") == "mine"


@pytest.mark.skipif(sys.platform == "win32", reason="檔案連結只在 POSIX 測")
def test_writing_into_the_drive_does_not_follow_a_link(model_base, tmp_path):
    """舊程式：``drive/x`` 本身不存在、解析後在靈魂根裡就放行，``x.tmp`` 是連結時寫到另一端."""
    outside = tmp_path.resolve() / "outside.txt"
    outside.write_text("kept", encoding="utf-8")
    (model_base / "drive" / "x.tmp").symlink_to(outside)
    soul_io.write_soul_file(MODEL, "drive/x", "pwned")
    assert outside.read_text(encoding="utf-8") == "kept", LINK_FOLLOWED


@pytest.mark.parametrize(("ident", "fixture"), [(SOUL, "soul_base"), (MODEL, "model_base")])
def test_single_level_soul_files_still_work(request, ident, fixture):
    """今天的靈魂檔都是單層（``soul.md``、``config.json``）：讀寫、備份、還原、刪除照舊."""
    base = request.getfixturevalue(fixture)
    assert soul_io.write_soul_file(ident, "config.json", "{}") is True
    assert soul_io.write_soul_file(ident, "config.json", '{"a": 1}') is True
    assert (base / "config.json.bak").read_text(encoding="utf-8") == "{}"
    assert soul_io.undo_soul_edit(ident, "config.json") is True
    assert soul_io.read_soul_file(ident, "config.json") == "{}"
    assert soul_io.backup_soul_file(ident, "config.json") is True
    assert soul_io.soul_file_exists(ident, "config.json") is True
    assert soul_io.delete_soul_file(ident, "config.json") is True
    assert not (base / "config.json").exists()
