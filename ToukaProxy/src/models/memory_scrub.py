"""清除連備份的回應模型：逐檔結果、類別結果與整體報告（重置端點回應的 ``scrub``）."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ScrubKind = Literal["live_store", "upgrade_backup", "snapshot", "leftover", "category", "info"]
ScrubStatus = Literal["scrubbed", "not_needed", "removed", "failed", "info"]
ScrubCategoryName = Literal["leftovers", "live", "upgrade_backups", "snapshots"]
ScrubCategoryState = Literal["done", "failed", "not_started"]
# 原因代碼是固定清單；回應、稽核與畫面只用代碼，不帶任何路徑或列的值。
ScrubReason = Literal[
    "busy",
    "in_use",
    "read_only",
    "no_space",
    "unreadable",
    "too_large",
    "has_journal",
    "unknown_member",
    "unknown_file",
    "unknown_schema",
    "unknown_qdrant_meta",
    "collection_shared",
    "ambiguous_member",
    "changed_during_scrub",
    "verify_failed",
    "live_verify_failed",
    "write_failed",
    "rewritten_after_reset",
    "list_failed",
    "lock_timeout",
    "recheck_failed",
    "own_snapshot_missing",
    "own_snapshot_unverified",
    "reset_not_clean",
    "backup_failed",
    "cancelled",
    "internal_error",
    "empty_file",
    "out_of_scope",
    "os_metadata_file",
]
SCRUB_CATEGORIES: tuple[ScrubCategoryName, ...] = (
    "leftovers",
    "live",
    "upgrade_backups",
    "snapshots",
)
_OK_STATUSES = frozenset({"scrubbed", "not_needed", "removed", "info"})


class ScrubItemResult(BaseModel):
    """一個檔（或一個類別、一則資訊）的結果；name 只有檔名."""

    kind: ScrubKind
    name: str
    status: ScrubStatus
    reason: ScrubReason | None = None
    facts_removed: int = 0
    collection_removed: bool = False
    members_dropped: int = 0
    count: int | None = None


class ScrubCategoryResult(BaseModel):
    """一個類別有沒有確實跑完."""

    name: ScrubCategoryName
    state: ScrubCategoryState = "not_started"
    reason: ScrubReason | None = None


class MemoryScrubReport(BaseModel):
    """清除的整體報告.

    complete 只有在四類都跑完、每項都成功、重查為 0、自己剛做的快照在清單上才為真。
    """

    requested: bool = True
    ran: bool = False
    complete: bool = False
    reason: ScrubReason | None = None
    categories: list[ScrubCategoryResult] = Field(default_factory=list)
    items: list[ScrubItemResult] = Field(default_factory=list)
    own_snapshot: str | None = None
    memory_remaining: int | None = None
    leftovers_removed: int = 0

    @classmethod
    def skipped(cls, reason: ScrubReason) -> MemoryScrubReport:
        """勾了但沒有執行（例如重置沒有完全成功）."""
        return cls(requested=True, ran=False, complete=False, reason=reason)

    @classmethod
    def build(
        cls,
        *,
        categories: list[ScrubCategoryResult],
        items: list[ScrubItemResult],
        own_snapshot: str | None,
        memory_remaining: int | None,
        internal_error: bool,
        recheck_failed: bool,
        reset_removed: int = 0,
    ) -> MemoryScrubReport:
        """依結果算 complete 與 reason（O08、O08b 的對照表）.

        ``reset_removed``：這次重置清掉的記憶筆數。大於 0 表示重置前剛做的那份快照一定含目標的
        記憶，它必須真的被清掉東西（scrubbed 且刪了事實或拿掉 collection），否則不算完成。
        """
        own = next(
            (i for i in items if i.kind == "snapshot" and i.name == own_snapshot and own_snapshot),
            None,
        )
        own_listed = own is not None
        own_verified = own is not None and (
            reset_removed == 0
            or (own.status == "scrubbed" and (own.facts_removed > 0 or own.collection_removed))
        )
        failed_category = next((c for c in categories if c.state != "done"), None)
        complete = (
            not internal_error
            and failed_category is None
            and all(item.status in _OK_STATUSES for item in items)
            and memory_remaining == 0
            and own_listed
            and own_verified
        )
        reason: ScrubReason | None = None
        if not complete:
            if failed_category is not None:
                reason = failed_category.reason or "internal_error"
            elif internal_error:
                reason = "internal_error"
            elif recheck_failed:
                reason = "recheck_failed"
            elif not own_listed:
                reason = "own_snapshot_missing"
            elif not own_verified:
                reason = "own_snapshot_unverified"
        return cls(
            requested=True,
            ran=True,
            complete=complete,
            reason=reason,
            categories=categories,
            items=items,
            own_snapshot=own_snapshot,
            memory_remaining=memory_remaining,
            leftovers_removed=sum(
                1 for i in items if i.kind == "leftover" and i.status == "removed"
            ),
        )
