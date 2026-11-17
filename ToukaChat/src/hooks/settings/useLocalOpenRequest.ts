import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchTouka, getActiveProxyUrl } from '@/lib/connections';
import type { ErrorCode } from '@/lib/error_codes';
import { getResponseErrorCode, logSSOTError } from '@/lib/error_handler';
import type { LocalOpenResult } from '@/lib/types';

const OPEN_PATH = '/v1/local-bridge/open-requests';

/** 最多等多久：比 Touka 的 150 秒長，Touka 先給答案（ADR-0320）。 */
export const OPEN_REQUEST_TIMEOUT_MS = 170_000;

/**
 * 設定頁自己的畫面狀態（不是領域模型）：小幫手的結果、被拒絕、路徑不合格式、沒送出、等太久。
 */
export type OpenOutcome =
    | { kind: 'result'; result: LocalOpenResult }
    | { kind: 'denied'; code: ErrorCode | null }
    | { kind: 'invalid' }
    | { kind: 'failed' }
    | { kind: 'timeout' };

/** 去掉前後空白，以及檔案總管「複製為路徑」會帶的一組前後雙引號。 */
export function cleanFolderPath(raw: string): string {
    const trimmed = raw.trim();
    const quoted = trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"');
    return (quoted ? trimmed.slice(1, -1) : trimmed).trim();
}

/** 先看 HTTP 狀態：只有 403、409 才查代碼；401、5xx 都當沒收到結果。 */
async function outcomeFrom(res: Response): Promise<OpenOutcome> {
    if (res.ok) return { kind: 'result', result: (await res.json()) as LocalOpenResult };
    if (res.status === 403 || res.status === 409) {
        return { kind: 'denied', code: (await getResponseErrorCode(res)) ?? null };
    }
    if (res.status === 422) return { kind: 'invalid' };
    return { kind: 'failed' };
}

/**
 * 從設定頁請小幫手開放資料夾（ADR-0320）。小幫手一定跳視窗，人決定或 2 分鐘到才有結果，
 * 所以自帶中止訊號：離開頁面就中止，等太久就當成「可能已經開放」。
 */
export function useLocalOpenRequest() {
    const [pending, setPending] = useState(false);
    const [outcome, setOutcome] = useState<OpenOutcome | null>(null);
    const controllerRef = useRef<AbortController | null>(null);
    const mountedRef = useRef(true);

    useEffect(() => {
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
            controllerRef.current?.abort();
        };
    }, []);

    const requestOpen = useCallback(async (raw: string): Promise<void> => {
        const path = cleanFolderPath(raw);
        if (!path) return;
        const controller = new AbortController();
        controllerRef.current = controller;
        let timedOut = false;
        const timer = setTimeout(() => {
            timedOut = true;
            controller.abort();
        }, OPEN_REQUEST_TIMEOUT_MS);
        setPending(true);
        setOutcome(null);
        let next: OpenOutcome | null = null;
        try {
            const res = await fetchTouka(getActiveProxyUrl(), OPEN_PATH, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path }),
                signal: controller.signal,
            });
            next = await outcomeFrom(res);
        } catch (err) {
            if (timedOut) {
                next = { kind: 'timeout' };
            } else if (!controller.signal.aborted) {
                logSSOTError(err, 'useLocalOpenRequest');
                next = { kind: 'failed' };
            }
        } finally {
            clearTimeout(timer);
        }
        if (!mountedRef.current) return;
        if (next !== null) setOutcome(next);
        setPending(false);
    }, []);

    return { pending, outcome, requestOpen };
}
