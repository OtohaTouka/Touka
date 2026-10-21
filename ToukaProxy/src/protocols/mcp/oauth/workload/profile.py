"""Versioned non-secret P8-B workload profile repository。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from filelock import AsyncFileLock  # noqa: FILELOCK_ASYNC_SSOT
from src.protocols.mcp.oauth.workload.models import WorkloadProfilePolicy
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from src.protocols.mcp.oauth.workload.models import WorkloadOAuthProfile


class WorkloadProfileConflictError(ValueError):
    """A profile mutation violated generation compare-and-swap。"""


class WorkloadProfileDocumentError(ValueError):
    """The workload profile policy document is malformed。"""


class WorkloadProfileRepository:
    """Atomic ResourceManager JSON repository with cross-process mutation lock。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = AsyncFileLock(str(self._path) + ".lock", timeout=30)

    async def load(self) -> WorkloadProfilePolicy:
        """Load a strict policy snapshot; malformed existing files fail closed。"""
        exists = await asyncio.to_thread(resource_manager.exists, self._path)
        raw = await asyncio.to_thread(resource_manager.read_json, self._path)
        if raw is None:
            if exists:
                raise WorkloadProfileDocumentError()
            return WorkloadProfilePolicy(version="1.0.0", author="system")
        try:
            return WorkloadProfilePolicy.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise WorkloadProfileDocumentError() from exc

    async def get(self, profile_id: str) -> WorkloadOAuthProfile | None:
        """Return one exact profile without mutable cache state。"""
        return (await self.load()).profiles.get(profile_id)

    async def list_profiles(self) -> list[WorkloadOAuthProfile]:
        """Return deterministic non-secret profiles for the admin control plane。"""
        document = await self.load()
        return [document.profiles[key] for key in sorted(document.profiles)]

    async def put(
        self,
        profile: WorkloadOAuthProfile,
        *,
        version: str,
        author: str,
    ) -> WorkloadOAuthProfile:
        """Create generation one or replace using exact generation+1 CAS。"""
        async with self._lock:
            document = await self.load()
            existing = document.profiles.get(profile.profile_id)
            if (
                existing is None
                and profile.generation != 1
                or existing is not None
                and profile.generation != existing.generation + 1
            ):
                raise WorkloadProfileConflictError()
            profiles = dict(document.profiles)
            profiles[profile.profile_id] = profile
            updated = document.model_copy(
                update={
                    "version": version,
                    "author": author,
                    "profiles": profiles,
                }
            )
            await asyncio.to_thread(
                resource_manager.write_json,
                self._path,
                updated.model_dump(mode="json"),
                "utf-8",
                2,
            )
        return profile

    async def delete(
        self,
        profile_id: str,
        *,
        expected_generation: int,
        version: str,
        author: str,
    ) -> None:
        """Remove one exact generation using the same cross-process mutation lock。"""
        async with self._lock:
            document = await self.load()
            existing = document.profiles.get(profile_id)
            if existing is None or existing.generation != expected_generation:
                raise WorkloadProfileConflictError()
            profiles = dict(document.profiles)
            del profiles[profile_id]
            updated = document.model_copy(
                update={"version": version, "author": author, "profiles": profiles}
            )
            await asyncio.to_thread(
                resource_manager.write_json,
                self._path,
                updated.model_dump(mode="json"),
                "utf-8",
                2,
            )
