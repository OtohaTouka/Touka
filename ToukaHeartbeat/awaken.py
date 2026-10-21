"""Touka 智慧節律器 (Smart Pacemaker).

本模組作為 Touka 系統的核心脈衝驅動器 (Pulse-Driven Architecture)。
獨立負責所有代理人的自主喚醒排程、計時決策與脈衝強度控制。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response  # noqa: TC002
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

if __package__:
    from .config import settings
    from .errors import HeartbeatError
    from .models import HeartbeatSchedule
    from .proxy_client import ProxyClient
    from .schedule_service import ScheduleService
    from .storage_service import StorageService
    from .token_provider import StorageTokenProvider
else:
    from config import settings
    from errors import HeartbeatError
    from models import HeartbeatSchedule
    from proxy_client import ProxyClient
    from schedule_service import ScheduleService
    from storage_service import StorageService
    from token_provider import StorageTokenProvider

# 設定日誌記錄
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "heartbeat.log", encoding="utf-8", delay=True),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("ToukaHeartbeat")


def get_taipei_time_info(epoch_time: float | None = None) -> tuple[str, str]:
    """將 UNIX Epoch Time 轉換為台北時間顯示格式 (Presentation Layer)."""
    tz = ZoneInfo("Asia/Taipei")
    dt = datetime.fromtimestamp(epoch_time, tz) if epoch_time else datetime.now(UTC).astimezone(tz)

    time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    weekday_map = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    weekday = weekday_map[dt.weekday()]
    return time_str, weekday


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """管理應用程式生命週期 (Startup & Shutdown)。"""
    try:
        get_schedule_service().initialize()
        scheduler.start()
        logger.info("📡 [Heartbeat] Scheduler Engine started.")
    except Exception:
        get_schedule_service().startup_failed()
        logger.error(  # noqa: TRY400
            "Heartbeat configuration or engine requires recovery; startup remains degraded"
        )
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("📡 [Heartbeat] Scheduler Engine stopped.")


app = FastAPI(title="ToukaHeartbeat - Smart Pacemaker", lifespan=lifespan)


@app.exception_handler(HeartbeatError)
async def heartbeat_error_handler(_request: Request, error: HeartbeatError) -> JSONResponse:
    """Return only the domain's fixed, safe failure fields."""
    return JSONResponse(status_code=error.status_code, content={"detail": error.public_detail()})


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    _request: Request, _error: RequestValidationError
) -> JSONResponse:
    """Never echo original request input or validator context."""
    error = HeartbeatError("schedule_invalid", phase="request_validation")
    return JSONResponse(status_code=error.status_code, content={"detail": error.public_detail()})


# 允許 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_config_file(base_dir: Path, override: str) -> Path:
    """ADR-0150：解析設定檔路徑（SSOT）。

    HEARTBEAT_CONFIG_FILE 覆寫時指向「掛載的設定『目錄』」內的檔案（Docker 佈署——heartbeat
    改掛目錄而非單檔，避開缺席單檔被 Docker 自動建成目錄→「目錄疊檔案」啟動崩潰）；未設定
    （或空白）則回退 base_dir/config.json（非 Docker／既有行為不變）。
    """
    override = (override or "").strip()
    return Path(override) if override else base_dir / "config.json"


# 初始化服務與排程器
BASE_DIR = Path(__file__).parent
# 刻意於此（dev compose 有 live-mount 的 awaken.py）直接讀環境變數、不經 config.py——後者
# 已烤進映像，改欄位需重建映像；直讀 env 讓修復於 `docker compose up -d`（免 --build）即生效。
CONFIG_FILE = _resolve_config_file(BASE_DIR, os.environ.get("HEARTBEAT_CONFIG_FILE", ""))
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

storage = StorageService(CONFIG_FILE, env_token=settings.HEARTBEAT_TOKEN)
token_provider = StorageTokenProvider(storage)
proxy_client = ProxyClient(token_provider)


def verify_schedule_token(request: Request) -> None:
    """Verify Bearer token for schedule management endpoints (C9 fix).

    Uses the shared provider (env > config.json > auto-generated).
    Local fallback remains available for management. Raises HTTP 401 if the
    Authorization header is missing or the token does not match.
    """
    credential = token_provider.resolve()

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = auth_header[len("Bearer ") :]
    if not credential.accepts(token):
        raise HTTPException(status_code=401, detail="Invalid token")


scheduler = AsyncIOScheduler(
    timezone=TAIPEI_TZ,
    job_defaults={"misfire_grace_time": 3600, "coalesce": True, "max_instances": 3},
)

_schedule_service: ScheduleService | None = None


def get_schedule_service() -> ScheduleService:
    """Bind one coordinator to the existing trusted storage/scheduler instances."""
    global _schedule_service
    with storage.transaction_lock:
        if (
            _schedule_service is None
            or _schedule_service.storage is not storage
            or _schedule_service.scheduler is not scheduler
        ):
            _schedule_service = ScheduleService(
                storage,
                scheduler,
                proxy_client.trigger_heartbeat,
                lambda: datetime.now(UTC).timestamp(),
                TAIPEI_TZ,
            )
        return _schedule_service


class TriggerPayload(BaseModel):
    """手動觸發請求酬載."""

    token: str
    agent_id: str  # noqa: IDENTITY_SSOT
    pulse_count: int = 1
    pulse_slots: int | None = None
    session_id: str | None = "pulse_channel_default"
    session_mode: str | None = "independent"
    init_prompt: str | None = None
    security_level: str | None = "safe"
    model_override: str | None = None


@app.get("/v1/schedules", response_model=list[HeartbeatSchedule], response_model_exclude_unset=True)
async def list_schedules(
    response: Response,
    _auth: None = Depends(verify_schedule_token),  # noqa: B008
) -> list[dict[str, Any]]:
    """Return schedules and a canonical schedules-only revision."""
    schedules, revision = get_schedule_service().view()
    response.headers["ETag"] = revision
    return schedules


@app.post("/v1/schedules", response_model=HeartbeatSchedule)
async def upsert_schedule(
    sched: HeartbeatSchedule,
    if_match: str | None = Header(default=None),  # noqa: B008
    _auth: None = Depends(verify_schedule_token),  # noqa: B008
) -> dict[str, Any]:
    """Persist and apply a schedule, or report its precise failed outcome."""
    return get_schedule_service().upsert(sched, if_match)


@app.delete("/v1/schedules/{schedule_id:path}")
async def delete_schedule(
    schedule_id: str,
    if_match: str | None = Header(default=None),  # noqa: B008
    _auth: None = Depends(verify_schedule_token),  # noqa: B008
) -> dict[str, str]:
    """Delete one exact schedule under the same revision transaction lock."""
    get_schedule_service().delete(schedule_id, if_match)
    return {"status": "deleted"}


@app.post("/trigger")
async def handle_manual_trigger(payload: TriggerPayload) -> dict[str, str]:
    """手動觸發接口 (保持向下相容)."""
    credential = token_provider.resolve()
    if not credential.accepts(payload.token):
        raise HTTPException(status_code=401, detail="Invalid token")
    if not credential.shared_ready:
        raise HeartbeatError("shared_credential_not_ready", phase="credential")

    # 立即觸發
    asyncio.create_task(
        proxy_client.trigger_heartbeat(
            agent_id=payload.agent_id,
            pulse_count=payload.pulse_count,
            pulse_slots=payload.pulse_slots,
            init_prompt=payload.init_prompt,
            model_override=payload.model_override,
            security_level=payload.security_level,
            session_mode=payload.session_mode,
            session_id=payload.session_id,
        )
    )
    return {"status": "dispatched", "agent_id": payload.agent_id}


@app.get("/status")
async def get_status() -> dict[str, Any]:
    """獲取智慧節律器當前運行狀態與台北時間."""
    return {
        "status": "degraded" if get_schedule_service().degraded else "running",
        "persisted": get_schedule_service().persisted_state,
        "engine": get_schedule_service().engine_state,
        "active_jobs": len(scheduler.get_jobs()),
        "time_taipei": get_taipei_time_info()[0],
        "time_utc": datetime.now(UTC).isoformat(),
    }


if __name__ == "__main__":
    config_data = storage.load_config()
    server_port = config_data.get("trigger_port", 8082)
    logger.info("📡 [Heartbeat] Server starting on port %d", server_port)
    uvicorn.run(app, host="0.0.0.0", port=server_port)  # noqa: S104
