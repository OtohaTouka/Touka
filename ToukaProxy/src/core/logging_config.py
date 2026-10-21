"""日誌系統初始化配置 (Logging Configuration).

負責系統日誌的層級設定、輸出格式與滾動備份配置。
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import pin_sdk_loggers
from src.services.resource_manager import resource_manager


def setup_logging():
    """配置全域日誌系統.

    設定主控台輸出與系統滾動日誌檔案 (system_status.log)，限制單個日誌最大 5MB 且最多備份 3 個。

    Returns:
        logging.Logger: 設定完畢的 "ToukaProxy" 主 logger 實例。

    Raises:
        RuntimeError: 當無法建立日誌儲存目錄時拋出。
    """
    try:
        log_dir = Path(resource_manager.get_base_path()) / "logs"
        Path(log_dir).mkdir(parents=True, exist_ok=True)
    except Exception:
        raise RuntimeError("Failed to initialize log directory")

    sys_log_path = Path(log_dir) / "system_status.log"

    # Setup 5MB max size per file, back up 3 files
    sys_handler = RotatingFileHandler(
        sys_log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(), sys_handler],
    )
    pin_sdk_loggers()  # openai／httpx2 釘在 WARNING（ADR-0248 D4）

    return logging.getLogger("ToukaProxy")


def close_sandbox_log_handlers(directory: str | None = None) -> int:
    """關閉並卸除指向指定目錄之下的所有日誌 FileHandler.

    Windows 上未關閉的 RotatingFileHandler 會持續鎖住其日誌檔（如 logs/
    system_status.log、proxy_payload.log），使測試沙盒的 shutil.rmtree 失敗並
    拋出 WinError 32（檔案正由另一個程序使用）。在刪除沙盒前呼叫本函式，
    flush + close + 卸除這些 handler，讓 teardown 在 Windows 上具決定性。

    Args:
        directory: 僅關閉「日誌檔位於此目錄（含子目錄）之下」的 FileHandler。
            傳入 None 時關閉所有 FileHandler（完整關機情境）。

    Returns:
        int: 已關閉並卸除的 handler 數量。
    """
    target: Path | None = None
    if directory is not None:
        try:
            target = Path(directory).resolve()
        except OSError:
            target = Path(directory)

    # 根 logger ＋ 所有具名 logger（跳過尚未實體化的 PlaceHolder）。
    loggers: list[logging.Logger] = [logging.getLogger()]
    loggers.extend(
        obj
        for obj in logging.Logger.manager.loggerDict.values()
        if isinstance(obj, logging.Logger)
    )

    closed = 0
    for lg in loggers:
        for handler in list(getattr(lg, "handlers", [])):
            if not isinstance(handler, logging.FileHandler):
                continue
            if target is not None:
                try:
                    base = Path(handler.baseFilename).resolve()
                except (OSError, ValueError):
                    continue
                if base != target and target not in base.parents:
                    continue
            try:
                handler.acquire()
                try:
                    handler.flush()
                    handler.close()
                finally:
                    handler.release()
            except (OSError, ValueError):
                # teardown 必須容錯：handler 已關閉或磁碟錯誤都不應中斷清理。
                pass
            lg.removeHandler(handler)
            closed += 1
    return closed
