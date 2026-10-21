import { useState, useCallback } from 'react';
import { Tool } from '@/lib/types';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

export function useToolSettings() {
    const [systemTools, setSystemTools] = useState<Tool[]>([]);
    
    const fetchTools = useCallback(async () => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const tRes = await fetchTouka(proxyUrl, "/v1/tools");
            if (tRes.ok) setSystemTools((await tRes.json()).tools || []);
        } catch (e) {
            logSSOTError(e, "useToolSettings:fetchTools");
        }
    }, []);

    return { systemTools, setSystemTools, fetchTools };
}
