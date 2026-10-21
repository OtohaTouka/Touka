"""Tests for guard_noqa_registry.py — NOQA Registry Sync Guard.

ADR-0029: 驗證 NOQA_REGISTRY.md 統計表與代碼庫實際計數一致性。
使用 tmp_path + monkeypatch 完全隔離，不依賴真實 filesystem。
"""

import sys
from pathlib import Path

import pytest

# 動態導入 scripts/guard_noqa_registry.py
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from guard_noqa_registry import (  # noqa: E402
    compare,
    parse_registry_stats,
    scan_codebase,
)


# ── parse_registry_stats 測試 ─────────────────────────────────────────────────

SAMPLE_REGISTRY = """\
# NOQA Registry

## 全系統統計 (Global Statistics)

| 規則 ID | 數量 | 說明 |
|---------|------|------|
| `IO_PRISON_VIOLATION` | 6 | I/O 監獄違規 |
| `LOCAL_IMPORT_BAN` | 10 | 循環導入破壞點 |
| `IDENTITY_SSOT` | 5 | 身分物件化違規 |
| `BCRYPT_IMPORT_SSOT` | 0 | 目前無豁免 |
| 其他 (`S104`, `S105`, `S607`, `TC003` 等) | 14 | 安全類豁免 |

---
"""


def test_parse_registry_extracts_named_rules(tmp_path: Path) -> None:
    """統計表中具名 `RULE_ID` 行應被正確解析。"""
    registry = tmp_path / "NOQA_REGISTRY.md"
    registry.write_text(SAMPLE_REGISTRY, encoding="utf-8")
    result = parse_registry_stats(registry)
    assert result == {
        "IO_PRISON_VIOLATION": 6,
        "LOCAL_IMPORT_BAN": 10,
        "IDENTITY_SSOT": 5,
        "BCRYPT_IMPORT_SSOT": 0,
    }


def test_parse_registry_skips_composite_row(tmp_path: Path) -> None:
    """「其他 (`S104`, ...)」複合行不應被解析為具名規則。"""
    registry = tmp_path / "NOQA_REGISTRY.md"
    registry.write_text(SAMPLE_REGISTRY, encoding="utf-8")
    result = parse_registry_stats(registry)
    # 複合行的 rule 不應出現在結果中
    assert "S104" not in result
    assert "S105" not in result
    assert "其他" not in result


def test_parse_registry_empty_table(tmp_path: Path) -> None:
    """無統計表行的 registry 應回傳空 dict。"""
    registry = tmp_path / "NOQA_REGISTRY.md"
    registry.write_text("# NOQA Registry\n\nNo table here.\n", encoding="utf-8")
    result = parse_registry_stats(registry)
    assert result == {}


# ── scan_codebase 測試 ─────────────────────────────────────────────────────────

def test_scan_py_noqa_single_rule(tmp_path: Path) -> None:
    """Python 單一 # noqa: RULE_ID 應被計入。"""
    (tmp_path / "service.py").write_text(
        "path.mkdir()  # noqa: IO_PRISON_VIOLATION\n",
        encoding="utf-8",
    )
    result = scan_codebase(tmp_path)
    assert result.get("IO_PRISON_VIOLATION") == 1


def test_scan_py_noqa_multiple_rules(tmp_path: Path) -> None:
    """# noqa: RULE_A, RULE_B 應各自計數一次。"""
    (tmp_path / "tool.py").write_text(
        "subprocess.run(cmd)  # noqa: S603, TID251\n",
        encoding="utf-8",
    )
    result = scan_codebase(tmp_path)
    assert result.get("S603") == 1
    assert result.get("TID251") == 1


def test_scan_ts_noqa(tmp_path: Path) -> None:
    """TypeScript // noqa: FETCH_SSOT 應被計入。"""
    (tmp_path / "connections.ts").write_text(
        "return fetch(url)  // noqa: FETCH_SSOT\n",
        encoding="utf-8",
    )
    result = scan_codebase(tmp_path)
    assert result.get("FETCH_SSOT") == 1


def test_scan_tsx_noqa(tmp_path: Path) -> None:
    """TSX 檔案 // noqa: LOCALSTORAGE_SSOT 應被計入。"""
    (tmp_path / "ThemeContext.tsx").write_text(
        "const theme = localStorage.getItem('theme')  // noqa: LOCALSTORAGE_SSOT\n",
        encoding="utf-8",
    )
    result = scan_codebase(tmp_path)
    assert result.get("LOCALSTORAGE_SSOT") == 1


def test_ignored_dirs_excluded(tmp_path: Path) -> None:
    """位於 .venv/, __pycache__/ 的 noqa 不應計入。"""
    venv_dir = tmp_path / ".venv" / "lib"
    venv_dir.mkdir(parents=True)
    (venv_dir / "bcrypt.py").write_text(
        "import bcrypt  # noqa: BCRYPT_IMPORT_SSOT\n",
        encoding="utf-8",
    )
    cache_dir = tmp_path / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "cached.py").write_text(
        "x = 1  # noqa: LOCAL_IMPORT_BAN\n",
        encoding="utf-8",
    )
    result = scan_codebase(tmp_path)
    assert result.get("BCRYPT_IMPORT_SSOT", 0) == 0
    assert result.get("LOCAL_IMPORT_BAN", 0) == 0


def test_scan_no_noqa(tmp_path: Path) -> None:
    """無 noqa 的代碼庫應回傳空 dict。"""
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    result = scan_codebase(tmp_path)
    assert result == {}


# ── compare 測試 ────────────────────────────────────────────────────────────────

def test_registry_in_sync_passes() -> None:
    """實際計數與預期完全一致 → 無錯誤。"""
    expected = {"IO_PRISON_VIOLATION": 6, "LOCAL_IMPORT_BAN": 10}
    actual   = {"IO_PRISON_VIOLATION": 6, "LOCAL_IMPORT_BAN": 10}
    errors = compare(expected, actual)
    assert errors == []


def test_registry_count_too_low_detected() -> None:
    """實際數 > 登記數（registry 未更新）→ 報差異。"""
    expected = {"IDENTITY_SSOT": 5}
    actual   = {"IDENTITY_SSOT": 8}
    errors = compare(expected, actual)
    assert len(errors) == 1
    assert "IDENTITY_SSOT" in errors[0]
    assert "5" in errors[0] and "8" in errors[0]


def test_registry_count_too_high_detected() -> None:
    """實際數 < 登記數（noqa 已移除但 registry 未更新）→ 報差異。"""
    expected = {"LOCAL_IMPORT_BAN": 10}
    actual   = {"LOCAL_IMPORT_BAN": 7}
    errors = compare(expected, actual)
    assert len(errors) == 1
    assert "LOCAL_IMPORT_BAN" in errors[0]


def test_zero_count_in_registry_with_actual_zero_passes() -> None:
    """registry 記 0 且代碼庫確實 0 → 通過（白名單規則無需 noqa）。"""
    expected = {"BCRYPT_IMPORT_SSOT": 0}
    actual   = {}  # 代碼庫中不存在此 noqa
    errors = compare(expected, actual)
    assert errors == []


def test_unlisted_architecture_rule_detected() -> None:
    """代碼中出現 registry 未追蹤的架構型 rule（含底線）→ 報錯。"""
    expected = {"IO_PRISON_VIOLATION": 6}
    actual   = {"IO_PRISON_VIOLATION": 6, "NEW_ARCH_RULE": 3}
    errors = compare(expected, actual)
    # NEW_ARCH_RULE 含底線 → 視為架構型，應報告未登記
    assert any("NEW_ARCH_RULE" in e for e in errors)


def test_untracked_standard_ruff_rule_no_error() -> None:
    """代碼中出現未登記的純 Ruff 格式規則（如 B027）→ 不報錯（不屬架構型）。"""
    expected = {"IO_PRISON_VIOLATION": 6}
    actual   = {"IO_PRISON_VIOLATION": 6, "B027": 2}
    errors = compare(expected, actual)
    # B027 是純 Ruff 規則（無底線，字母+數字格式），不應報錯
    assert not any("B027" in e for e in errors)
