import { useEffect, useState } from "react";
import { fetchTouka } from "@/lib/connections";
import { logSSOTError } from "@/lib/error_handler";
import { catalogFromToolsStats, type SoulToolCatalog } from "@/lib/soul_tools";

const LOADING: SoulToolCatalog = { status: "loading", tools: [], companions: {} };
const FAILED: SoulToolCatalog = { status: "failed", tools: [], companions: {} };

/** 靈魂工具勾選清單（`/v1/tools_stats`，ADR-0281 T2）；換連線就重讀，別的連線讀到的清單不給。 */
export function useSoulToolCatalog(proxyUrl: string): SoulToolCatalog {
    const [loaded, setLoaded] = useState<{ proxyUrl: string; catalog: SoulToolCatalog } | null>(null);

    useEffect(() => {
        let current = true;
        void (async () => {
            let catalog = FAILED;
            try {
                const res = await fetchTouka(proxyUrl, "/v1/tools_stats");
                if (res.ok) catalog = catalogFromToolsStats(await res.json());
            } catch (err) {
                logSSOTError(err, "useSoulToolCatalog");
            }
            if (current) setLoaded({ proxyUrl, catalog });
        })();
        return () => { current = false; };
    }, [proxyUrl]);

    return loaded?.proxyUrl === proxyUrl ? loaded.catalog : LOADING;
}
