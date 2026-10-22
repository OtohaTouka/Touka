"""Python 套件 7 天冷卻期（model-p0 U5）。

專案規則「新套件要等七天」：root ``pyproject.toml`` 的 ``[tool.uv].exclude-newer``
固定為 ``"7 days"``，重生的 ``uv.lock`` 只記錄期間（``exclude-newer-span = "P7D"``）。
改這個值、或用 ``exclude-newer-package`` 為任何套件開例外，都要 Owner 逐一同意。
只涵蓋 uv 管理的 Python 套件；前端 npm 不在內。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_sets_seven_day_cooldown():
    uv = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]
    assert uv.get("exclude-newer") == "7 days"
    assert "exclude-newer-package" not in uv, "套件例外要 Owner 逐一同意"


def test_lockfile_records_cooldown_span():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert lock.get("options", {}).get("exclude-newer-span") == "P7D"
    assert "exclude-newer-package" not in lock.get("options", {}), "套件例外要 Owner 逐一同意"
