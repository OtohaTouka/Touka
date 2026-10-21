"""Safe Heartbeat domain errors backed by the generated public error registry."""

from typing import Any

if __package__:
    from .error_codes import ERRORS
else:
    from error_codes import ERRORS


class HeartbeatError(Exception):
    """Carry fixed failure categories and transaction state, never raw input."""

    def __init__(
        self,
        reason: str,
        *,
        phase: str | None = None,
        committed: bool | None = False,
        durability: str = "NOT_CONFIRMED",
        persisted: str | None = None,
        engine: str | None = None,
    ) -> None:
        self.reason = reason
        self.code = f"HEARTBEAT_{reason.upper()}"
        definition = ERRORS[self.code]
        self.status_code = definition["status"]
        self.phase = phase
        self.committed = committed
        self.durability = durability
        self.persisted = persisted
        self.engine = engine
        super().__init__(definition["msg"])

    def public_detail(self) -> dict[str, Any]:
        """Return fixed registry text and classified recovery state."""
        definition = ERRORS[self.code]
        return {
            "code": self.code,
            "id": definition["id"],
            "message": definition["msg"],
            "i18n": definition["i18n"],
            "reason": self.reason,
            "phase": self.phase,
            "committed": self.committed,
            "durability": self.durability,
            "persisted": self.persisted,
            "engine": self.engine,
        }
