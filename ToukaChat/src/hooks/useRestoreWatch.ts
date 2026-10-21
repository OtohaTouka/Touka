import { useEffect } from "react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { logSSOTError } from "@/lib/error_handler";
import type { RestoreStatus } from "@/lib/types";

/** 送出還原後多久第一次查狀態（伺服器成功後會重啟）。 */
const FIRST_CHECK_MS = 15000;
/** 還原還在跑時，隔多久再查一次。 */
const RECHECK_MS = 5000;

/** 查最後一次還原的結果；查不到（伺服器重啟中、連不上、未授權）回 null。 */
async function fetchRestoreStatus(): Promise<RestoreStatus | null> {
    try {
        const res = await fetchTouka(getActiveProxyUrl(), "/v1/system/restore/status");
        return res.ok ? ((await res.json()) as RestoreStatus) : null;
    } catch (e) {
        logSSOTError(e, "useRestoreWatch:fetchRestoreStatus");
        return null;
    }
}

/**
 * H1：還原中，重整頁面前先查狀態。
 * failed → 呼叫 onFailed、不重整；running → 稍後再查；
 * 其他（成功後伺服器重啟、查不到）→ 照舊重整。onFailed 需穩定（useCallback）。
 */
export function useRestoreWatch(active: boolean, onFailed: () => void): void {
    useEffect(() => {
        if (!active) return;
        let cancelled = false;
        let timer: ReturnType<typeof setTimeout> | undefined;
        const check = async () => {
            const status = await fetchRestoreStatus();
            if (cancelled) return;
            if (status?.state === "failed") onFailed();
            else if (status?.state === "running") timer = setTimeout(check, RECHECK_MS);
            else window.location.reload();
        };
        timer = setTimeout(check, FIRST_CHECK_MS);
        return () => {
            cancelled = true;
            clearTimeout(timer);
        };
    }, [active, onFailed]);
}
