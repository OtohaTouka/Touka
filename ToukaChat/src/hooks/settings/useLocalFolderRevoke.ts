import { useCallback, useState } from 'react';
import { fetchTouka, getActiveProxyUrl } from '@/lib/connections';
import type { ErrorCode } from '@/lib/error_codes';
import { getResponseErrorCode, logSSOTError } from '@/lib/error_handler';
import type { LocalFolderEntry, LocalRevokeRequest, LocalRevokeResult } from '@/lib/types';

const REVOKE_PATH = '/v1/local-bridge/folders/revoke';

/**
 * 設定頁自己的畫面狀態（不是領域模型）：小幫手的結果、被拒絕、沒收到結果。
 * 沒收到結果時不知道小幫手有沒有改，畫面照「不確定」說。
 */
export type RevokeOutcome =
    | { kind: 'result'; result: LocalRevokeResult }
    | { kind: 'denied'; code: ErrorCode | null }
    | { kind: 'failed' };

async function outcomeFrom(res: Response): Promise<RevokeOutcome> {
    if (res.ok) return { kind: 'result', result: (await res.json()) as LocalRevokeResult };
    if (res.status === 403 || res.status === 409) {
        return { kind: 'denied', code: (await getResponseErrorCode(res)) ?? null };
    }
    return { kind: 'failed' };
}

/**
 * 請小幫手把開放的資料夾拿掉或改成只能讀（ADR-0330 2b）。只能縮小；小幫手驗章後直接做，
 * 不跳視窗。名稱與路徑照小幫手回報的那一筆送回去。
 */
export function useLocalFolderRevoke() {
    const [pending, setPending] = useState(false);
    const [outcome, setOutcome] = useState<RevokeOutcome | null>(null);

    const revoke = useCallback(
        async (folder: Pick<LocalFolderEntry, 'name' | 'path'>, change: LocalRevokeRequest['change']): Promise<RevokeOutcome> => {
            setPending(true);
            setOutcome(null);
            let next: RevokeOutcome;
            try {
                const body: LocalRevokeRequest = { name: folder.name, path: folder.path, change };
                const res = await fetchTouka(getActiveProxyUrl(), REVOKE_PATH, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                next = await outcomeFrom(res);
            } catch (err) {
                logSSOTError(err, 'useLocalFolderRevoke');
                next = { kind: 'failed' };
            }
            setOutcome(next);
            setPending(false);
            return next;
        },
        [],
    );

    return { pending, outcome, revoke };
}
