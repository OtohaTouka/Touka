"""Bounded MCP ``subscriptions/listen`` lifecycle supervisor."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcp.client.subscriptions import (
    PromptsListChanged,
    ResourcesListChanged,
    ResourceUpdated,
    SubscriptionLost,
    ToolsListChanged,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

Sleep = Callable[[float], Awaitable[None]]
EventCallback = Callable[[str], Awaitable[None] | None]


class McpSubscriptionSupervisor:
    """Own one change stream bound to one installation/handle principal."""

    def __init__(
        self,
        client: Any,
        *,
        tools_list_changed: bool,
        prompts_list_changed: bool,
        resources_list_changed: bool,
        resource_subscriptions: Sequence[str] = (),
        max_backoff_seconds: float = 30.0,
        sleep: Sleep = asyncio.sleep,
        on_event: EventCallback | None = None,
    ) -> None:
        if max_backoff_seconds <= 0:
            message = "max_backoff_seconds must be positive"
            raise ValueError(message)
        self._client = client
        self._filters = {
            "tools_list_changed": tools_list_changed,
            "prompts_list_changed": prompts_list_changed,
            "resources_list_changed": resources_list_changed,
            "resource_subscriptions": tuple(resource_subscriptions),
        }
        self._max_backoff_seconds = max_backoff_seconds
        self._sleep = sleep
        self._on_event = on_event
        self._task: asyncio.Task[None] | None = None
        self._status = "starting"
        self._tracked_features = {
            feature
            for feature, enabled in (
                ("tools", tools_list_changed),
                ("prompts", prompts_list_changed),
                (
                    "resources",
                    resources_list_changed or bool(resource_subscriptions),
                ),
            )
            if enabled
        }
        self._stale_features: set[str] = set()
        self._reconnect_attempts = 0
        self._last_event: str | None = None
        self._event_count = 0

    def start(self) -> asyncio.Task[None]:
        """Start background supervision without blocking the MCP connection."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        """Stop only the listen stream; the underlying MCP client remains usable."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._status = "stopped"

    async def replace_resource_subscriptions(self, uris: Sequence[str]) -> None:
        """Restart listen when the set of resource URIs changes."""
        wanted = tuple(dict.fromkeys(uris))
        if wanted == self._filters["resource_subscriptions"]:
            return
        self._filters["resource_subscriptions"] = wanted
        await self.stop()
        self._status = "starting"
        self.start()

    def mark_fresh(self, feature: str) -> None:
        """A successful feature refetch clears only that feature's stale bit."""
        self._stale_features.discard(feature)

    async def run(self) -> None:
        """Relisten forever with capped backoff until stopped or ack is invalid."""
        backoff_attempt = 0
        while True:
            event_count_before = self._event_count
            keep_running = await self.run_once()
            if not keep_running:
                return
            self._stale_features.update(self._tracked_features)
            self._status = "retrying"
            self._reconnect_attempts += 1
            if self._event_count > event_count_before:
                backoff_attempt = 0
            delay = min(2**backoff_attempt, self._max_backoff_seconds)
            await self._sleep(float(delay))
            backoff_attempt += 1

    async def run_once(self) -> bool:
        """Run one acknowledged stream. False means invalid ack and no retry."""
        try:
            async with self._client.listen(**self._filters) as subscription:
                if not self._valid_ack(subscription):
                    self._status = "invalid_filter"
                    self._stale_features.update(self._tracked_features)
                    return False
                self._status = "listening"
                async for event in subscription:
                    kind = self._event_kind(event)
                    if kind is None:
                        continue
                    self._last_event = kind
                    self._event_count += 1
                    self._stale_features.add(kind.partition("/")[0])
                    if self._on_event is not None:
                        result = self._on_event(kind)
                        if inspect.isawaitable(result):
                            await result
        except SubscriptionLost:
            self._stale_features.update(self._tracked_features)
        except Exception:
            # Listener failure is an observability/freshness failure, never a
            # reason to tear down the otherwise healthy MCP client.
            self._status = "error"
            self._stale_features.update(self._tracked_features)
        return True

    def _valid_ack(self, subscription: Any) -> bool:
        subscription_id = getattr(subscription, "subscription_id", None)
        if not isinstance(subscription_id, str | int) or subscription_id == "":
            return False
        honored = getattr(subscription, "honored", None)
        if honored is None:
            return False
        for field in (
            "tools_list_changed",
            "prompts_list_changed",
            "resources_list_changed",
        ):
            if bool(getattr(honored, field, False)) and not bool(self._filters[field]):
                return False
        requested_uris = set(self._filters["resource_subscriptions"])
        honored_uris = set(getattr(honored, "resource_subscriptions", None) or ())
        return honored_uris <= requested_uris

    @staticmethod
    def _event_kind(event: Any) -> str | None:
        if isinstance(event, ToolsListChanged):
            return "tools/list_changed"
        if isinstance(event, PromptsListChanged):
            return "prompts/list_changed"
        if isinstance(event, ResourcesListChanged):
            return "resources/list_changed"
        if isinstance(event, ResourceUpdated):
            return "resources/updated"
        return None

    def snapshot(self) -> dict[str, Any]:
        """Return payload-free freshness diagnostics."""
        return {
            "status": self._status,
            "stale": bool(self._stale_features),
            "stale_features": sorted(self._stale_features),
            "reconnect_attempts": self._reconnect_attempts,
            "last_event": self._last_event,
            "resource_subscription_count": len(
                self._filters["resource_subscriptions"]
            ),
        }
