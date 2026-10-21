"""Single-process schedule transactions: validate, persist, apply, compensate."""

import copy
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import ValidationError

if __package__:
    from .errors import HeartbeatError
    from .models import HeartbeatConfig, HeartbeatSchedule, parse_daily_time, schedules_revision
    from .storage_service import StorageService
else:
    from errors import HeartbeatError
    from models import HeartbeatConfig, HeartbeatSchedule, parse_daily_time, schedules_revision
    from storage_service import StorageService

logger = logging.getLogger("ToukaHeartbeat.Schedules")


class SchedulerPort(Protocol):
    """The existing APScheduler operations required by one local transaction."""

    @property
    def running(self) -> bool:
        """Report whether the scheduler has started."""
        ...

    def get_jobs(self) -> list[Any]:
        """Return currently registered jobs."""
        ...

    def remove_job(self, job_id: str) -> None:
        """Remove one exact job identity."""
        ...

    def add_job(self, func: Callable, trigger: Any, **kwargs: Any) -> Any:
        """Register a callable with validated trigger and options."""
        ...

    def pause(self) -> None:
        """Suspend scheduler dispatch."""
        ...


@dataclass(frozen=True)
class JobPlan:
    """Validated engine input for one exact job."""

    job_id: str
    trigger: Any
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class JobSnapshot:
    """Existing callable, trigger and options used for compensation."""

    func: Callable
    trigger: Any
    options: dict[str, Any]


class ScheduleService:
    """Coordinate one writer; no await/network occurs while holding its lock."""

    def __init__(
        self,
        storage: StorageService,
        scheduler: SchedulerPort,
        callback: Callable,
        current_epoch: Callable[[], float],
        timezone: ZoneInfo,
    ) -> None:
        self.storage = storage
        self.scheduler = scheduler
        self.callback = callback
        self.current_epoch = current_epoch
        self.timezone = timezone
        self.degraded = False
        self.persisted_state = "UNKNOWN"
        self.engine_state = "UNKNOWN"
        self.startup_job_ids: set[str] = set()

    async def dispatch(self, **kwargs: Any) -> Any:
        """Block newly admitted jobs after degradation; await outside the lock."""
        with self.storage.transaction_lock:
            if self.degraded:
                return 503
        return await self.callback(**kwargs)

    def _require_ready(self) -> None:
        if self.degraded:
            raise HeartbeatError(
                "schedule_recovery_required",
                persisted=self.persisted_state,
                engine=self.engine_state,
            )

    def _pause(self) -> None:
        self.degraded = True
        try:
            if self.scheduler.running:
                self.scheduler.pause()
                self.engine_state = "PAUSED"
            else:
                self.engine_state = "STOPPED"
        except Exception:
            self.engine_state = "UNKNOWN"

    def view(self) -> tuple[list[dict[str, Any]], str]:
        """Read validated schedules and their content revision."""
        with self.storage.transaction_lock:
            self._require_ready()
            try:
                config = self.storage.load_config()
            except HeartbeatError:
                self.persisted_state = "UNKNOWN"
                self._pause()
                raise
            schedules = config.get("schedules", [])
            return schedules, schedules_revision(schedules)

    def _read_snapshot(self) -> tuple[dict[str, Any], bytes]:
        try:
            return self.storage.read_snapshot()
        except HeartbeatError:
            self.persisted_state = "UNKNOWN"
            self._pause()
            raise

    def startup_failed(self) -> None:
        """Clean only this startup's planned jobs and deny further dispatch."""
        with self.storage.transaction_lock:
            try:
                self._remove_exact(self.startup_job_ids)
            except Exception:
                logger.warning("Heartbeat startup job cleanup failed")
            self._pause()

    def plan(self, config: dict[str, Any]) -> dict[str, list[JobPlan]]:
        """Validate all triggers and globally unique job IDs without engine I/O."""
        try:
            model = HeartbeatConfig.model_validate(config)
            plans = {schedule.id: self._plan_schedule(schedule) for schedule in model.schedules}
            identifiers = [job.job_id for jobs in plans.values() for job in jobs]
            if len(set(identifiers)) != len(identifiers):
                raise HeartbeatError("schedule_invalid", phase="plan")
        except (ValidationError, ValueError, OverflowError, OSError):
            raise HeartbeatError("schedule_invalid", phase="plan") from None
        return plans

    def _plan_schedule(self, schedule: HeartbeatSchedule) -> list[JobPlan]:
        if not schedule.active:
            return []
        identifier = schedule.id
        assert identifier is not None
        prefix = f"hb:{len(identifier)}:{identifier}:"
        arguments = {
            "agent_id": schedule.agent_id,
            "pulse_count": schedule.pulse_count,
            "pulse_slots": schedule.pulse_slots,
            "init_prompt": schedule.init_prompt,
            "model_override": schedule.model_override,
            "security_level": schedule.security_level,
            "session_mode": schedule.session_mode,
            "session_id": schedule.session_id,
        }
        if schedule.mode == "interval":
            trigger = IntervalTrigger(minutes=schedule.interval_minutes, timezone=self.timezone)
            return [JobPlan(prefix + "interval", trigger, arguments)]
        if schedule.mode == "daily":
            jobs = []
            for value in schedule.daily_times or []:
                hour, minute = parse_daily_time(value)
                trigger = CronTrigger(hour=hour, minute=minute, timezone=self.timezone)
                jobs.append(JobPlan(prefix + f"daily:{hour:02d}{minute:02d}", trigger, arguments))
            return jobs
        assert schedule.once_datetime is not None
        when = datetime.fromtimestamp(schedule.once_datetime / 1000, self.timezone)
        if schedule.once_datetime / 1000 <= self.current_epoch():
            return []
        return [
            JobPlan(prefix + "once", DateTrigger(run_date=when, timezone=self.timezone), arguments)
        ]

    def _snapshot_jobs(self, affected: set[str]) -> list[JobSnapshot]:
        snapshots = []
        for job in self.scheduler.get_jobs():
            if job.id not in affected:
                continue
            options = {"id": job.id, "args": job.args, "kwargs": copy.deepcopy(job.kwargs)}
            for name in (
                "name",
                "misfire_grace_time",
                "coalesce",
                "max_instances",
                "executor",
                "next_run_time",
            ):
                if hasattr(job, name):
                    options[name] = getattr(job, name)
            snapshots.append(JobSnapshot(job.func, job.trigger, options))
        return snapshots

    def _remove_exact(self, identifiers: set[str]) -> None:
        for job in list(self.scheduler.get_jobs()):
            if job.id in identifiers:
                self.scheduler.remove_job(job.id)

    def _apply(self, affected: set[str], planned: list[JobPlan]) -> None:
        self._remove_exact(affected)
        for job in planned:
            self.scheduler.add_job(
                self.dispatch,
                job.trigger,
                kwargs=job.kwargs,
                id=job.job_id,
                misfire_grace_time=3600,
                coalesce=True,
                max_instances=3,
            )

    def initialize(self) -> None:
        """Validate full configuration before installing any startup jobs."""
        with self.storage.transaction_lock:
            self._require_ready()
            affected: set[str] = set()
            try:
                config = self.storage.load_config()
                planned = [job for jobs in self.plan(config).values() for job in jobs]
                affected = {job.job_id for job in planned}
                self.startup_job_ids = affected
                self._apply(affected, planned)
                self.persisted_state = "CURRENT"
                self.engine_state = "CURRENT"
            except Exception:
                try:
                    self._remove_exact(affected)
                except Exception:
                    logger.warning("Heartbeat partial startup cleanup failed")
                self._pause()
                raise HeartbeatError(
                    "schedule_recovery_required", phase="startup", engine=self.engine_state
                ) from None

    def _compensate(
        self,
        original: bytes,
        affected: set[str],
        jobs: list[JobSnapshot],
        cause: HeartbeatError,
    ) -> None:
        persisted_ok = False
        engine_ok = False
        try:
            self.storage.restore_bytes(original)
            persisted_ok = True
        except Exception:
            logger.warning("Heartbeat configuration compensation failed")
        try:
            self._remove_exact(affected)
            for job in jobs:
                self.scheduler.add_job(job.func, job.trigger, **job.options)
            engine_ok = True
        except Exception:
            logger.warning("Heartbeat engine compensation failed")
        self.persisted_state = "OLD" if persisted_ok else "UNKNOWN"
        self.engine_state = "OLD" if engine_ok else "UNKNOWN"
        if not (persisted_ok and engine_ok):
            self._pause()
            raise HeartbeatError(
                "schedule_recovery_required",
                phase="compensation",
                committed=cause.committed,
                persisted=self.persisted_state,
                engine=self.engine_state,
            ) from None
        raise HeartbeatError(
            cause.reason,
            phase=cause.phase,
            committed=cause.committed,
            persisted="OLD",
            engine="OLD",
        ) from None

    def _commit(
        self, old: bytes, candidate: dict[str, Any], affected: set[str], jobs: list[JobPlan]
    ) -> None:
        snapshots = self._snapshot_jobs(affected)
        try:
            self.storage.save_config(candidate)
        except HeartbeatError as failure:
            if failure.committed is False:
                raise
            self._compensate(old, affected, snapshots, failure)
        try:
            self._apply(affected, jobs)
        except Exception:
            self._compensate(
                old,
                affected,
                snapshots,
                HeartbeatError("schedule_apply_failed", phase="engine", committed=True),
            )
        self.persisted_state = "CURRENT"
        self.engine_state = "CURRENT"

    def _check_revision(self, config: dict[str, Any], expected: str | None) -> None:
        if expected is not None and expected != schedules_revision(config.get("schedules", [])):
            raise HeartbeatError("revision_conflict", phase="revision")

    def upsert(
        self, schedule: HeartbeatSchedule, expected_revision: str | None = None
    ) -> dict[str, Any]:
        """Apply one revision-checked schedule change or compensate it."""
        with self.storage.transaction_lock:
            self._require_ready()
            config, original = self._read_snapshot()
            self._check_revision(config, expected_revision)
            old_plans = self.plan(config)
            candidate = copy.deepcopy(config)
            schedules = candidate.setdefault("schedules", [])
            identifier = schedule.id or uuid.uuid4().hex
            updates = schedule.model_dump(exclude_unset=True)
            updates["id"] = identifier
            index = next((i for i, row in enumerate(schedules) if row["id"] == identifier), None)
            if index is None:
                saved = updates
                schedules.append(saved)
            else:
                saved = {**schedules[index], **updates}
                schedules[index] = saved
            new_plans = self.plan(candidate)
            affected = {job.job_id for job in old_plans.get(identifier, []) + new_plans[identifier]}
            self._commit(original, candidate, affected, new_plans[identifier])
            return HeartbeatSchedule.model_validate(saved).model_dump()

    def delete(self, identifier: str, expected_revision: str | None = None) -> None:
        """Remove one schedule in the same persistence/engine transaction."""
        with self.storage.transaction_lock:
            self._require_ready()
            config, original = self._read_snapshot()
            self._check_revision(config, expected_revision)
            old_plans = self.plan(config)
            if identifier not in old_plans:
                raise HeartbeatError("schedule_not_found")
            candidate = copy.deepcopy(config)
            candidate["schedules"] = [row for row in config["schedules"] if row["id"] != identifier]
            self.plan(candidate)
            affected = {job.job_id for job in old_plans[identifier]}
            self._commit(original, candidate, affected, [])
