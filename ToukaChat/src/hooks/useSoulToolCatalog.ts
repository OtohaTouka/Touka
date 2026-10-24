import { useEffect, useState } from "react";
import { fetchTouka } from "@/lib/connections";
import { logSSOTError } from "@/lib/error_handler";
import { catalogFromToolsStats, type SoulToolEntry } from "@/lib/soul_tools";

const EMPTY: SoulToolEntry[] = [];

/** 靈魂工具勾選清單（`/v1/tools_stats`，ADR-0281 T2）；換連線就重讀，讀不到時是空清單。 */
export function useSoulToolCatalog(proxyUrl: string): SoulToolEntry[] {
    const [loaded, setLoaded] = useState<{ proxyUrl: string; catalog: SoulToolEntry[] } | null>(null);

    useEffect(() => {
        let current = true;
        void (async () => {
            try {
                const res = await fetchTouka(proxyUrl, "/v1/tools_stats");
                if (!res.ok) return;
                const catalog = catalogFromToolsStats(await res.json());
                if (current) setLoaded({ proxyUrl, catalog });
            } catch (err) {
                logSSOTError(err, "useSoulToolCatalog");
            }
        })();
        return () => { current = false; };
    }, [proxyUrl]);

    // 別的連線讀到的清單不給
    return loaded?.proxyUrl === proxyUrl ? loaded.catalog : EMPTY;
}
