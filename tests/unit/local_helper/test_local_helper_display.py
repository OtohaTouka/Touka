"""小幫手視窗的縮放與字型（ADR-0312）：純計算，Linux 也測得到。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from touka_local import winapi
from touka_local.display import (
    BASE_DPI,
    UI_FONT_FAMILY,
    DisplayScale,
    pick_font_family,
    scale_factor,
    scaled,
)


@pytest.mark.parametrize(
    ("pixels_per_inch", "expected"),
    [(96.0, 1.0), (120.0, 1.25), (144.0, 1.5), (192.0, 2.0), (72.0, 1.0), (0.0, 1.0)],
)
def test_scale_factor_follows_the_screen_but_never_shrinks(
    pixels_per_inch: float, expected: float
) -> None:
    assert scale_factor(pixels_per_inch) == expected


def test_base_dpi_is_windows_default() -> None:
    assert BASE_DPI == 96.0


@pytest.mark.parametrize(
    ("value", "factor", "expected"), [(780, 2.0, 1560), (12, 1.25, 15), (1, 0.5, 1), (0, 2.0, 0)]
)
def test_scaled_rounds_and_keeps_positive_sizes_visible(
    value: int, factor: float, expected: int
) -> None:
    assert scaled(value, factor) == expected


def test_display_scale_px_uses_its_factor() -> None:
    assert DisplayScale(factor=2.0, family="x").px(130) == 260
    assert DisplayScale().px(130) == 130


def test_prefers_microsoft_jhenghei_ui() -> None:
    assert pick_font_family(["Arial", UI_FONT_FAMILY, "PMingLiU"]) == UI_FONT_FAMILY


def test_falls_back_to_the_default_font_when_missing() -> None:
    assert pick_font_family(["Arial", "DejaVu Sans"]) == ""


def test_font_tuple_uses_the_chosen_family() -> None:
    assert DisplayScale(factor=1.0, family=UI_FONT_FAMILY).font(12, "bold") == (
        UI_FONT_FAMILY,
        12,
        "bold",
    )
    assert DisplayScale().font(11) == ("", 11)


# --- 告訴 Windows 小幫手會自己處理縮放 ---


class _Recorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail = fail

    def __call__(self, *args: Any) -> int:
        self.calls.append(args)
        if self.fail:
            raise OSError
        return 0


def test_dpi_awareness_uses_system_aware_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    shcore = SimpleNamespace(SetProcessDpiAwareness=_Recorder())
    user32 = SimpleNamespace(SetProcessDPIAware=_Recorder())
    monkeypatch.setattr(winapi, "_load_dpi_libraries", lambda: (shcore, user32))
    assert winapi.enable_dpi_awareness() is True
    assert shcore.SetProcessDpiAwareness.calls == [(winapi.PROCESS_SYSTEM_DPI_AWARE,)]
    assert user32.SetProcessDPIAware.calls == []


def test_dpi_awareness_falls_back_to_the_older_call(monkeypatch: pytest.MonkeyPatch) -> None:
    shcore = SimpleNamespace(SetProcessDpiAwareness=_Recorder(fail=True))
    user32 = SimpleNamespace(SetProcessDPIAware=_Recorder())
    monkeypatch.setattr(winapi, "_load_dpi_libraries", lambda: (shcore, user32))
    assert winapi.enable_dpi_awareness() is True
    assert user32.SetProcessDPIAware.calls == [()]


def test_dpi_awareness_never_stops_the_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    shcore = SimpleNamespace(SetProcessDpiAwareness=_Recorder(fail=True))
    user32 = SimpleNamespace(SetProcessDPIAware=_Recorder(fail=True))
    monkeypatch.setattr(winapi, "_load_dpi_libraries", lambda: (shcore, user32))
    assert winapi.enable_dpi_awareness() is False


def test_dpi_awareness_is_a_no_op_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winapi, "_load_dpi_libraries", lambda: None)
    assert winapi.enable_dpi_awareness() is False
