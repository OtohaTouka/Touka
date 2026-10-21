"use client";

import { Fragment, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
    Layers, RefreshCw, Download, Copy, Check,
    ChevronRight, ChevronDown, Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useContextSnapshot, type SnapshotBlock } from "@/hooks/useContextSnapshot";

const _AGENT = "DebugAgent";

function kindClass(kind: SnapshotBlock["kind"]): string {
    if (kind === "real") return "text-status-success";
    if (kind === "mock") return "text-status-warning";
    return "text-muted-foreground";
}

function tierClass(tier: SnapshotBlock["cache_tier"]): string {
    if (tier === "stable") return "text-primary";
    if (tier === "volatile") return "text-muted-foreground";
    return "text-muted-foreground";
}

/**
 * Context Snapshot 面板（Debug Center 第 4 子分頁，ADR-0133）。
 *
 * 預先檢視 DebugAgent「組裝完成的系統提示」：後端跑真實 pipeline（停在模型前、純讀），
 * 依 tag SSOT 有序解析各區塊，標註注入層／來源／真實或 mock／token 成本／快取分層。
 * 純唯讀、不打模型、不花費 API。可匯出 .md、複製完整組裝、對照最近一筆實際送出。
 */
export function ContextSnapshotPanel() {
    const { t } = useTranslation();
    const { status, snapshot, error, load, exportMarkdown } = useContextSnapshot();
    const [expanded, setExpanded] = useState<Set<number>>(new Set());
    const [copied, setCopied] = useState(false);
    const [withPayload, setWithPayload] = useState(false);
    const [showPayload, setShowPayload] = useState(false);

    // 掛載即載入一次（純唯讀、不花費）。
    useEffect(() => { void load(_AGENT, false); }, [load]);

    const loading = status === "loading";

    const handleRefresh = () => { void load(_AGENT, withPayload); };

    const toggle = (pos: number) => {
        setExpanded((prev) => {
            const next = new Set(prev);
            if (next.has(pos)) next.delete(pos); else next.add(pos);
            return next;
        });
    };

    const handleCopy = () => {
        if (!snapshot) return;
        void navigator.clipboard.writeText(snapshot.system_text).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        });
    };

    return (
        <div className="space-y-4">
            <p className="text-xs text-muted-foreground leading-relaxed">
                {t("contextSnapshot.description")}
            </p>

            <div className="flex items-center gap-2">
                <Button size="sm" className="h-8 font-bold flex-1" onClick={handleRefresh} disabled={loading}>
                    {loading
                        ? <Loader2 className="h-3.5 w-3.5 mr-2 animate-spin" />
                        : <RefreshCw className="h-3.5 w-3.5 mr-2" />}
                    {loading ? t("contextSnapshot.loading") : t("contextSnapshot.refresh")}
                </Button>
                {snapshot && (
                    <>
                        <Button size="sm" variant="outline" className="h-8 text-xs" onClick={handleCopy} title={t("contextSnapshot.copy")}>
                            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                        </Button>
                        <Button size="sm" variant="outline" className="h-8 text-xs" onClick={() => void exportMarkdown(_AGENT)} title={t("contextSnapshot.export")}>
                            <Download className="h-3.5 w-3.5" />
                        </Button>
                    </>
                )}
            </div>

            <label htmlFor="ctx-with-payload" className="flex items-center gap-2 cursor-pointer">
                <input
                    id="ctx-with-payload"
                    type="checkbox"
                    checked={withPayload}
                    onChange={(e) => setWithPayload(e.target.checked)}
                    className="accent-primary"
                />
                <span className="text-xs text-muted-foreground">{t("contextSnapshot.with_payload")}</span>
            </label>

            {error && <p className="text-xs text-destructive">{t("contextSnapshot.error")}: {error}</p>}

            {snapshot && (
                <>
                    <div className="space-y-1.5 rounded-lg border border-border bg-muted/20 p-3 text-xs">
                        <div className="flex items-center gap-2 font-bold">
                            <Layers className="h-3.5 w-3.5 text-primary shrink-0" />
                            <span className="font-mono">{snapshot.agent_id} · {snapshot.model}</span>
                        </div>
                        <p className="text-muted-foreground tabular-nums">
                            {t("contextSnapshot.tokens", {
                                total: snapshot.total_tokens,
                                window: snapshot.window,
                                pct: snapshot.usage_pct,
                            })}
                        </p>
                        <p className={snapshot.cache_reordered ? "text-status-warning" : "text-status-success"}>
                            {snapshot.cache_reordered
                                ? t("contextSnapshot.reordered_yes")
                                : t("contextSnapshot.reordered_no")}
                        </p>
                    </div>

                    <p className="text-xs text-muted-foreground leading-relaxed">
                        <span className="text-status-success font-bold">real</span> / <span className="text-status-warning font-bold">mock</span> / per_request ·{" "}
                        <span className="text-primary font-bold">stable</span> {t("contextSnapshot.tier_stable_hint")} · <span className="font-bold">volatile</span> {t("contextSnapshot.tier_volatile_hint")}
                    </p>

                    <div className="overflow-x-auto">
                        <table className="w-full text-xs">
                            <thead>
                                <tr className="text-left text-muted-foreground">
                                    <th className="py-1 pr-2 font-medium">{t("contextSnapshot.col_block")}</th>
                                    <th className="py-1 pr-2 font-medium">{t("contextSnapshot.col_kind")}</th>
                                    <th className="py-1 pr-2 font-medium">{t("contextSnapshot.col_tokens")}</th>
                                    <th className="py-1 font-medium">{t("contextSnapshot.col_cache")}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {snapshot.blocks.map((b) => {
                                    const isOpen = expanded.has(b.position);
                                    return (
                                        <Fragment key={b.position}>
                                            <tr
                                                className={`border-t border-border/50 cursor-pointer hover:bg-muted/30 ${b.present ? "" : "opacity-60"}`}
                                                onClick={() => toggle(b.position)}
                                            >
                                                <td className="py-1 pr-2">
                                                    <span className="inline-flex items-center gap-1">
                                                        {isOpen
                                                            ? <ChevronDown className="h-3 w-3 shrink-0" />
                                                            : <ChevronRight className="h-3 w-3 shrink-0" />}
                                                        <span className="font-bold">{b.position}. {b.block_name}</span>
                                                        {!b.present && (
                                                            <span className="text-status-warning shrink-0">
                                                                ⚪ {t("contextSnapshot.not_present_badge")}
                                                            </span>
                                                        )}
                                                    </span>
                                                </td>
                                                <td className={`py-1 pr-2 font-bold ${kindClass(b.kind)}`}>{b.kind}</td>
                                                <td className="py-1 pr-2 tabular-nums">{b.tokens}</td>
                                                <td className={`py-1 font-bold ${tierClass(b.cache_tier)}`}>{b.cache_tier ?? "—"}</td>
                                            </tr>
                                            {isOpen && (
                                                <tr className="bg-muted/10">
                                                    <td colSpan={4} className="py-2 px-2 space-y-1 break-all">
                                                        <div><span className="text-muted-foreground">layer: </span><span className="font-mono">{b.layer}</span></div>
                                                        <div><span className="text-muted-foreground">source: </span><span className="font-mono">{b.source}</span></div>
                                                        {!b.present && (
                                                            <div className="text-status-warning">{t("contextSnapshot.not_present")}</div>
                                                        )}
                                                        {b.preview && (
                                                            <pre className="mt-1 rounded bg-muted/40 p-1.5 whitespace-pre-wrap font-mono text-xs">{b.preview}</pre>
                                                        )}
                                                    </td>
                                                </tr>
                                            )}
                                        </Fragment>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>

                    {snapshot.last_payload && (
                        <div className="space-y-1.5">
                            <Button
                                size="sm"
                                variant="outline"
                                className="h-7 text-xs w-full"
                                onClick={() => setShowPayload((v) => !v)}
                            >
                                {showPayload ? <ChevronDown className="h-3 w-3 mr-1.5" /> : <ChevronRight className="h-3 w-3 mr-1.5" />}
                                {t("contextSnapshot.last_payload_title")}
                            </Button>
                            {showPayload && (
                                <pre className="rounded bg-muted/40 p-2 text-xs font-mono whitespace-pre-wrap break-all max-h-64 overflow-y-auto">
                                    {JSON.stringify(snapshot.last_payload, null, 2)}
                                </pre>
                            )}
                        </div>
                    )}
                </>
            )}
        </div>
    );
}
