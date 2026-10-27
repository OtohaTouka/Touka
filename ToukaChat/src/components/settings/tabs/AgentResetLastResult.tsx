"use client";

import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { LastResetStatus } from "@/lib/types";
import { ERRORS, type ErrorCode } from "@/lib/error_codes";
import { errorMsg } from "@/lib/error_handler";
import { formatToTaipeiTime } from "@/lib/time_utils";

/** 伺服器給的是 UNIX 秒；畫面一律顯示台北時間. */
export function formatResetTime(seconds: number | null | undefined): string {
    return seconds == null ? "" : formatToTaipeiTime(seconds * 1000);
}

/** 卡上要顯示的狀態（idle 與看不懂的狀態不顯示）. */
const SHOWN_STATES: ReadonlySet<string> = new Set(["running", "done", "failed", "interrupted"]);

export function isShownLastResult(last: LastResetStatus | null): last is LastResetStatus {
    return last != null && SHOWN_STATES.has(last.state);
}

function failureReason(t: TFunction, code: string | null | undefined): string {
    // 快照沒做成時重置根本沒執行：和送出時的拒絕用同一句話
    if (code === "SYSTEM_BACKUP_FAILED") return t("settings.agent_reset.backup_failed");
    return errorMsg(code && Object.hasOwn(ERRORS, code) ? (code as ErrorCode) : "SYSTEM_ERROR");
}

/** 哪個靈魂、什麼時候（還在跑寫開始時間，其餘寫結束時間）. */
export function lastResultHeading(t: TFunction, last: LastResetStatus): string {
    const id = last.agent_id ?? "";
    if (last.state === "running") {
        return t("settings.agent_reset.last.heading_running", { id, time: formatResetTime(last.started_at) });
    }
    return t("settings.agent_reset.last.heading_finished", { id, time: formatResetTime(last.finished_at) });
}

/** 結果一句話；看不懂的狀態當成查不到. */
export function lastResultLine(t: TFunction, last: LastResetStatus): string {
    switch (last.state) {
        case "running":
            return t("settings.agent_reset.last.running");
        case "done":
            return t("settings.agent_reset.last.done");
        case "failed":
            return t("settings.agent_reset.last.failed", { reason: failureReason(t, last.error_code) });
        case "interrupted":
            return t("settings.agent_reset.last.interrupted");
        default:
            return t("settings.agent_reset.last.none");
    }
}

/**
 * 重置卡上方：伺服器記著的最後一次重置（舊審查報告丙-5，ADR-0289）。
 * 只在伺服器記憶體裡，重啟後就沒有；結束的那次可以打開看明細，還在跑時按同一個按鈕再查一次。
 */
export function AgentResetLastResult({
    last,
    onView,
    onRefresh,
}: {
    last: LastResetStatus;
    onView: () => void;
    onRefresh: () => void;
}) {
    const { t } = useTranslation();
    const action = last.state === "done" && last.report ? onView : last.state === "running" ? onRefresh : null;
    return (
        <div className="flex items-center justify-between gap-3 rounded-2xl border border-border bg-background/50 p-3 text-xs">
            <div className="min-w-0 space-y-0.5">
                <p className="font-bold text-foreground">{lastResultHeading(t, last)}</p>
                <p className="text-muted-foreground">{lastResultLine(t, last)}</p>
            </div>
            {action && (
                <button
                    onClick={action}
                    className="shrink-0 rounded-xl border border-border px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-foreground"
                >
                    {t("settings.agent_reset.view_result")}
                </button>
            )}
        </div>
    );
}
