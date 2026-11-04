import { useEffect, useState } from 'react';
import { fetchTouka, getActiveProxyUrl } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

const ACTIVE_PATH = '/v1/hooks/active';

/**
 * 有沒有啟用中的 hook 會收到對話內容（ADR-0292）。
 *
 * 只在 `enabled` 時查；`recheckKey` 變了就再查一次（hook 在對話中途打開時，下一則就知道）。
 * 查不到時維持上一次的結果。
 */
export function useHooksActive(enabled: boolean, recheckKey: unknown): boolean {
    const [active, setActive] = useState(false);

    useEffect(() => {
        if (!enabled) return undefined;
        let cancelled = false;
        void (async () => {
            try {
                const res = await fetchTouka(getActiveProxyUrl(), ACTIVE_PATH);
                const body = res.ok ? await res.json() : null;
                if (!cancelled && typeof body?.active === 'boolean') setActive(body.active);
            } catch (err) {
                logSSOTError(err, 'useHooksActive');
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [enabled, recheckKey]);

    return enabled && active;
}
