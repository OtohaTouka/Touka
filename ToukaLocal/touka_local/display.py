"""小幫手視窗的縮放與字型（ADR-0312）。只做計算、不碰 tkinter，Linux 也測得到。

Windows 螢幕縮放大於 100% 時，沒宣告會自己處理縮放的程式會被系統當成圖片放大，字就糊了。
宣告之後字型會跟著放大，但用像素寫的視窗大小、欄寬不會，所以要乘上這裡算出的倍數。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

# Windows 100% 縮放時的每英吋像素
BASE_DPI: Final = 96.0
# 繁體中文介面字型；沒裝的電腦退回系統預設
UI_FONT_FAMILY: Final = "Microsoft JhengHei UI"
# 表格每列在字高之外多留的像素（未縮放）
TREE_ROW_PADDING: Final = 6


def scale_factor(pixels_per_inch: float) -> float:
    """螢幕每英吋幾個像素換成放大倍數；比 100% 小的一律當 100%。"""
    if pixels_per_inch <= BASE_DPI:
        return 1.0
    return pixels_per_inch / BASE_DPI


def scaled(value: int, factor: float) -> int:
    """像素值乘上倍數；正數至少留 1，0 照舊是 0。"""
    if value <= 0:
        return value
    return max(1, round(value * factor))


def pick_font_family(available: Iterable[str]) -> str:
    """有微軟正黑體就用；空字串代表用 Tk 的預設字型。"""
    return UI_FONT_FAMILY if UI_FONT_FAMILY in set(available) else ""


@dataclass(frozen=True)
class DisplayScale:
    """這台螢幕的放大倍數與字型。"""

    factor: float = 1.0
    family: str = ""

    def px(self, value: int) -> int:
        """把以 100% 縮放寫的像素換成這台螢幕的像素。"""
        return scaled(value, self.factor)

    def font(self, size: int, *styles: str) -> tuple[str | int, ...]:
        """Tk 的字型寫法；大小用點數，宣告縮放後系統會自己放大。"""
        return (self.family, size, *styles)
