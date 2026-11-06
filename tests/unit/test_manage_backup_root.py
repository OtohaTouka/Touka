"""``manage_backup.find_project_root`` 單元測試（ADR-0052 多-.env 根偵測）。

回歸測試：當 root/.env 有 SANCTUARY_DATA_PATH、子專案 .env 沒有時，從子專案
scripts 目錄往上找，必須回傳含該 key 的 ROOT，而非第一個遇到的子專案 .env。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "scripts" / "manage_backup.py"
)


def _load_manage_backup():
    """以路徑載入獨立腳本（非套件），避免依賴 sys.path 設定。"""
    spec = importlib.util.spec_from_file_location("manage_backup_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skips_subproject_env_without_key(tmp_path):
    """子專案 .env 缺 SANCTUARY_DATA_PATH 時，要爬到含該 key 的 ROOT。"""
    (tmp_path / ".env").write_text(
        "SANCTUARY_DATA_PATH=D:/ToukaSanctuary/data\n", encoding="utf-8"
    )
    scripts = tmp_path / "ToukaProxy" / "scripts"
    scripts.mkdir(parents=True)
    (tmp_path / "ToukaProxy" / ".env").write_text("PORT=8000\n", encoding="utf-8")

    mod = _load_manage_backup()
    assert mod.find_project_root(scripts) == tmp_path.resolve()


def test_climbs_past_multiple_keyless_envs(tmp_path):
    """連續多層子專案 .env 都缺 key 時，仍要一路爬到 ROOT。"""
    (tmp_path / ".env").write_text("SANCTUARY_DATA_PATH=D:/x\n", encoding="utf-8")
    deep = tmp_path / "ToukaProxy" / "scripts"
    deep.mkdir(parents=True)
    (tmp_path / "ToukaProxy" / ".env").write_text("A=1\n", encoding="utf-8")
    (deep / ".env").write_text("B=2\n", encoding="utf-8")

    mod = _load_manage_backup()
    assert mod.find_project_root(deep) == tmp_path.resolve()


def test_returns_dir_that_directly_has_key(tmp_path):
    """起點目錄自身的 .env 就含 key 時，直接回傳該目錄。"""
    (tmp_path / ".env").write_text('SANCTUARY_DATA_PATH="D:/q"\n', encoding="utf-8")

    mod = _load_manage_backup()
    assert mod.find_project_root(tmp_path) == tmp_path.resolve()


def test_collect_items_includes_legacy(tmp_path, zone_data_root):
    """collect_items 必須涵蓋 Sanctuary 的 legacy 子目錄（回歸保護）。

    data 根取自 ``zone_data_root``（ADR-0293：腳本碰到區的 I/O 測試絆線看得到）。
    """
    data = zone_data_root
    (data / "legacy" / "Alice").mkdir(parents=True)
    (data / "legacy" / "Alice" / "old.json").write_text("{}", encoding="utf-8")
    (data / "vault").mkdir()
    (data / "vault" / "core_memory.db").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"SANCTUARY_DATA_PATH={data.as_posix()}\n", encoding="utf-8"
    )

    mod = _load_manage_backup()
    engine = mod.BackupEngine(mod.PathResolver(tmp_path))
    arcs = {str(arc).replace("\\", "/") for _src, arc in engine.collect_items()}

    assert "data/legacy/Alice/old.json" in arcs
    assert "data/vault/core_memory.db" in arcs


def test_collect_items_includes_root_level_dbs(tmp_path, zone_data_root):
    """ADR-0104：data 根層的 ui_sessions.db / feedback.db 必須納入備份。

    這是「備份優先」名副其實的前提——DebugAgent 重置會刪 sessions/feedback，若備份漏了
    它們，刪除即不可還原。回歸保護：未來別再讓根層 DB 漏出備份範圍。data 根取自
    ``zone_data_root``。
    """
    data = zone_data_root
    (data / "ui_sessions.db").write_text("x", encoding="utf-8")
    (data / "feedback.db").write_text("x", encoding="utf-8")
    (data / "notes.txt").write_text("x", encoding="utf-8")  # 非 .db：不應被納入
    (tmp_path / ".env").write_text(
        f"SANCTUARY_DATA_PATH={data.as_posix()}\n", encoding="utf-8"
    )

    mod = _load_manage_backup()
    engine = mod.BackupEngine(mod.PathResolver(tmp_path))
    arcs = {str(arc).replace("\\", "/") for _src, arc in engine.collect_items()}

    assert "data/ui_sessions.db" in arcs
    assert "data/feedback.db" in arcs
    assert "data/notes.txt" not in arcs  # 僅 *.db 根層檔納入，範圍刻意收斂
