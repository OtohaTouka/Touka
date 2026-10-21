"""The pytest summary must keep failures and errors and also list every skip reason."""

from __future__ import annotations

import configparser
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# -ra / -rA expand to these report characters (pytest's own definition).
_REPORT_GROUPS = {"a": "sxXEf", "A": "PpsxXEf"}


def test_pytest_ini_reports_failures_errors_and_skip_reasons() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "pytest.ini", encoding="utf-8")
    report_options = [
        option[2:]
        for option in shlex.split(parser["pytest"].get("addopts", ""))
        if option.startswith("-r")
    ]

    assert report_options, "pytest.ini addopts must set -r"
    # -r replaces pytest's default "fE" instead of adding to it; the last one wins.
    chars = "".join(_REPORT_GROUPS.get(char, char) for char in report_options[-1])
    assert {"f", "E", "s"} <= set(chars)
