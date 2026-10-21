import { useCallback, useState } from "react";
import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { logSSOTError, errorMsg } from "@/lib/error_handler";

/** 單一組裝區塊（對齊後端 context_snapshot_service.build_snapshot）。 */
export interface SnapshotBlock {
    position: number;
    tag: string | null;
    block_name: string;
    layer: string;
    source: string;
    kind: "real" | "mock" | "per_request";
    present: boolean;
    content_length: number | null;
    tokens: number;
    preview: string | null;
    cache_tier: "stable" | "volatile" | null;
}

/** 完整組裝快照（對齊後端 build_snapshot 回傳）。 */
export interface ContextSnapshot {
    agent_id: string;
    model: string;
    window: number;
    total_tokens: number;
    usage_pct: number;
    /** 穩定塊原本被困在波動後 → 送出前重排（ADR-0130 生效）。 */
    cache_reordered: boolean;
    /** 完整組裝的 system 字串（供完整檢視／複製）。 */
    system_text: string;
    blocks: SnapshotBlock[];
    /** include_last_payload=true 時併回最近一筆實際送出的 payload。 */
    last_payload: Record<string, unknown> | null;
}

export type SnapshotStatus = "idle" | "loading" | "done" | "error";

export interface UseContextSnapshotReturn {
    status: SnapshotStatus;
    snapshot: ContextSnapshot | null;
    error: string | null;
    load: (agentId: string, includeLastPayload?: boolean) => Promise<void>;
    exportMarkdown: (agentId: string) => Promise<void>;
}

/**
 * Context Snapshot 讀取 hook（ADR-0133；Debug Center「Context」子分頁）。
 *
 * GET /v1/debug/context-snapshot：後端跑真實 ProxyPipeline 組裝（停在模型前、純讀）→
 * 回結構化區塊快照。純唯讀、不打模型、不花費 API。exportMarkdown 取 .md 匯出檔並下載。
 */
export function useContextSnapshot(): UseContextSnapshotReturn {
    const [status, setStatus] = useState<SnapshotStatus>("idle");
    const [snapshot, setSnapshot] = useState<ContextSnapshot | null>(null);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async (agentId: string, includeLastPayload = false) => {
        setStatus("loading");
        setError(null);
        const proxyUrl = getActiveProxyUrl();
        try {
            const qs = new URLSearchParams({
                agent_id: agentId,
                include_last_payload: String(includeLastPayload),
            });
            const res = await fetchTouka(proxyUrl, `/v1/debug/context-snapshot?${qs}`, {
                method: "GET",
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = (await res.json()) as ContextSnapshot;
            setSnapshot(data);
            setStatus("done");
        } catch (e) {
            logSSOTError(e, "useContextSnapshot:load");
            setError(errorMsg("SYSTEM_ERROR"));
            setStatus("error");
        }
    }, []);

    const exportMarkdown = useCallback(async (agentId: string) => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const qs = new URLSearchParams({ agent_id: agentId });
            const res = await fetchTouka(
                proxyUrl,
                `/v1/debug/context-snapshot/export?${qs}`,
                { method: "GET" },
            );
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const md = await res.text();
            const blob = new Blob([md], { type: "text/markdown" });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = `context-snapshot-${agentId}.md`;
            link.click();
            URL.revokeObjectURL(url);
        } catch (e) {
            logSSOTError(e, "useContextSnapshot:export");
        }
    }, []);

    return { status, snapshot, error, load, exportMarkdown };
}
