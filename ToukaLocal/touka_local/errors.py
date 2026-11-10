"""小幫手的拒絕訊號：規則不讓做的事都用它，帶代碼不帶絕對路徑。"""

from __future__ import annotations


class Refused(Exception):
    """規則不讓做。``detail`` 只放請求裡的相對路徑，會送回給模型。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
