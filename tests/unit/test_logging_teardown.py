"""Sandbox 日誌 handler 拆除測試（Windows teardown 決定性）。

驗證 close_sandbox_log_handlers：在刪除測試沙盒前，關閉並卸除指向沙盒目錄的
FileHandler，使 shutil.rmtree 不再因 RotatingFileHandler 仍持有 logs/*.log 而
WinError 32（程序無法存取檔案，因為檔案正由另一個程序使用）；同時不誤傷沙盒
目錄外的 handler。對應 conftest.global_test_sandbox 的清理路徑。
"""

import logging
import shutil
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.core.logging_config import close_sandbox_log_handlers


def _attach_rotating_handler(logger_name: str, log_path: Path) -> RotatingFileHandler:
    """掛載一個指向 log_path 的 RotatingFileHandler 並立即開啟檔案 handle。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path, maxBytes=1024, backupCount=1, encoding="utf-8"
    )
    logging.getLogger(logger_name).addHandler(handler)
    return handler


def test_closes_detaches_and_unlocks_handlers_under_directory():
    """沙盒內的 FileHandler 應被關閉、卸除，且沙盒隨後可被 rmtree 刪除。"""
    sandbox = Path(tempfile.mkdtemp(prefix="touka_logteardown_"))
    try:
        lg = logging.getLogger("touka_test_inside")
        handler = _attach_rotating_handler(
            "touka_test_inside", sandbox / "logs" / "system_status.log"
        )
        lg.warning("hold an open handle on the log file")

        closed = close_sandbox_log_handlers(str(sandbox))

        assert closed >= 1
        assert handler not in lg.handlers
        # handle 已釋放 → Windows 上對沙盒的 rmtree 不應再 WinError 32
        shutil.rmtree(sandbox)
        assert not sandbox.exists()
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_leaves_handlers_outside_directory_untouched():
    """沙盒目錄外的 FileHandler 不應被關閉或卸除。"""
    sandbox = Path(tempfile.mkdtemp(prefix="touka_logteardown_in_"))
    outside = Path(tempfile.mkdtemp(prefix="touka_logteardown_out_"))
    lg = logging.getLogger("touka_test_outside")
    outside_handler = _attach_rotating_handler("touka_test_outside", outside / "x.log")
    try:
        close_sandbox_log_handlers(str(sandbox))

        assert outside_handler in lg.handlers
    finally:
        outside_handler.close()
        lg.removeHandler(outside_handler)
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def test_returns_zero_when_no_matching_handler():
    """沙盒內沒有任何 FileHandler 時應回傳 0（無副作用）。"""
    sandbox = Path(tempfile.mkdtemp(prefix="touka_logteardown_empty_"))
    try:
        assert close_sandbox_log_handlers(str(sandbox)) == 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
