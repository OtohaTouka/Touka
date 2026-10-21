"use client";

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Eraser, ShieldAlert, Loader2, Bot } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import {
    Dialog,
    DialogContent,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";
import { useAgentDataReset, type AgentResetOutcome, type ResettableAgent } from "@/hooks/useAgentDataReset";
import type { AgentResetResponse } from "@/lib/types";
import { AgentResetScrubOption, NO_SCRUB, type ScrubChoice } from "./AgentResetScrubOption";
import { AgentResetScrubResult } from "./AgentResetScrubResult";
import { BackupRetentionNotice } from "@/components/common/BackupRetentionNotice";

type ResetView = { kind: "unknown" } | { kind: "done"; report: AgentResetResponse; scrubRequested: boolean };

/**
 * ADR-0113：Per-Agent 資料重置卡（admin only）。清除單一代理人累積的執行期資料
 * （對話/記憶/統計/任務/產出），**保留身分**（靈魂/系統提示）。打字確認 + 密碼重輸。
 * 可選「一併清除備份裡的記憶」（預設不勾、要第二段確認）；結果一律依回應裡的 scrub 判斷。
 */
export function AgentDataResetCard() {
    const { t } = useTranslation();
    const { agents, loadAgents, busy, reset } = useAgentDataReset();
    const [target, setTarget] = useState<ResettableAgent | null>(null);
    const [typed, setTyped] = useState("");
    const [password, setPassword] = useState("");
    const [scrub, setScrub] = useState<ScrubChoice>(NO_SCRUB);
    const [view, setView] = useState<ResetView | null>(null);

    const openFor = (agent: ResettableAgent) => {
        setTarget(agent);
        setTyped("");
        setPassword("");
        setScrub(NO_SCRUB);
        setView(null);
    };

    const close = () => {
        if (busy) return;
        setTarget(null);
        setTyped("");
        setPassword("");
        setScrub(NO_SCRUB);
    };

    const rejectionMessage = (outcome: Extract<AgentResetOutcome, { kind: "rejected" }>) => {
        // 快照沒做成時重置根本沒執行：講清楚，不只顯示籠統的「系統備份失敗」。
        if (outcome.code === "SYSTEM_BACKUP_FAILED") return t("settings.agent_reset.backup_failed");
        if (outcome.code === "MEMORY_SCRUB_COLLECTION_SHARED" && outcome.collidesWith) {
            return t("settings.agent_reset.scrub.collection_shared", { id: outcome.collidesWith });
        }
        return outcome.message;
    };

    const doReset = async () => {
        if (!target) return;
        const scrubRequested = scrub.checked;
        const outcome = await reset(target.id, {
            confirm: true,
            confirm_agent_id: target.id,
            current_password: password,
            scrub_backups: scrubRequested,
            scrub_acknowledged: scrubRequested && scrub.acknowledged,
        });
        if (outcome.kind === "unknown") {
            setView({ kind: "unknown" });
            return;
        }
        if (outcome.kind === "rejected") {
            toast.error(rejectionMessage(outcome));
            return;
        }
        const { report } = outcome;
        setView({ kind: "done", report, scrubRequested });
        // 勾了清除時，清除也完成才算成功（一律看回應裡的 scrub，不看自己送了什麼）。
        const scrubComplete = !scrubRequested || report.scrub?.complete === true;
        if (report.clean && scrubComplete) toast.success(t("settings.agent_reset.reset_success", { id: target.id }));
        else if (report.clean) toast.error(t("settings.agent_reset.scrub.toast_incomplete", { id: target.id }));
        else toast.error(t("settings.agent_reset.reset_partial", { id: target.id }));
    };

    const scrubReady = !scrub.checked || scrub.acknowledged;
    const canSubmit = !!target && typed === target.id && password.length > 0 && scrubReady && !busy;

    const finish = () => {
        close();
        loadAgents();
    };

    const doneButton = (
        <div className="flex justify-end">
            <button onClick={finish} className="rounded-xl bg-primary-button px-4 py-2 text-xs font-black uppercase tracking-widest text-primary-foreground hover:opacity-90">
                {t("settings.agent_reset.done")}
            </button>
        </div>
    );

    const renderResult = (current: ResetView) => {
        if (current.kind === "unknown") {
            return (
                <div className="mt-2 space-y-2 text-xs">
                    <p className="font-bold text-status-warning">{t("settings.agent_reset.result_unknown")}</p>
                    {doneButton}
                </div>
            );
        }
        const { report, scrubRequested } = current;
        const scrubRan = report.scrub?.ran === true;
        return (
            <div className="mt-2 space-y-2 text-xs">
                <p className={cn("font-bold", report.clean ? "text-status-success" : "text-status-warning")}>
                    {!report.clean
                        ? t("settings.agent_reset.result_partial")
                        : scrubRan
                          ? t("settings.agent_reset.scrub.result_clean_live")
                          : t("settings.agent_reset.result_clean")}
                </p>
                <ul className="space-y-0.5 text-muted-foreground">
                    {(report.stores ?? []).map((s) => (
                        <li key={s.name}>
                            {s.name}: {t("settings.agent_reset.store_line", { purged: s.purged, remaining: s.remaining })}
                            {s.error ? ` ⚠️ ${s.error}` : ""}
                        </li>
                    ))}
                </ul>
                <p className="text-muted-foreground">
                    {t("settings.agent_reset.preserved_line", { items: (report.retained ?? []).map((r) => r.name).join(", ") })}
                </p>
                {scrubRequested &&
                    (report.scrub?.requested ? (
                        <AgentResetScrubResult report={report.scrub} />
                    ) : (
                        <p className="font-bold text-status-warning">{t("settings.agent_reset.scrub.not_run")}</p>
                    ))}
                <BackupRetentionNotice variant={report.scrub?.complete === true ? "reset_scrubbed" : "reset"} agentId={target?.id ?? ""} />
                {doneButton}
            </div>
        );
    };

    return (
        <div className="rounded-3xl border border-border bg-muted/10 p-6 space-y-4">
            <div className="flex items-center gap-2">
                <Eraser className="w-5 h-5 text-status-warning" />
                <h4 className="text-sm font-black uppercase tracking-tight text-foreground">
                    {t("settings.agent_reset.title")}
                </h4>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
                {t("settings.agent_reset.intro")}
            </p>

            {agents.length === 0 ? (
                <p className="rounded-2xl border border-dashed border-border p-6 text-center text-xs italic text-muted-foreground">
                    {t("settings.agent_reset.empty")}
                </p>
            ) : (
                <div className="space-y-2">
                    {agents.map((a) => (
                        <div key={a.id} className="flex items-center justify-between gap-3 rounded-2xl border border-border bg-background/50 p-3">
                            <div className="flex min-w-0 items-center gap-2">
                                <Bot size={16} className="text-muted-foreground shrink-0" />
                                <span className="truncate font-bold text-foreground">{a.name || a.id}</span>
                                <span className="text-muted-foreground truncate font-mono text-xs">{a.id}</span>
                            </div>
                            <button
                                onClick={() => openFor(a)}
                                className="flex items-center gap-1 rounded-xl border border-status-warning/30 bg-status-warning/10 px-3 py-1.5 text-xs font-bold text-status-warning transition-all hover:bg-status-warning/20 shrink-0"
                            >
                                <Eraser size={12} /> {t("settings.agent_reset.reset_button")}
                            </button>
                        </div>
                    ))}
                </div>
            )}

            {/* 重置確認（打字確認 + 密碼重輸）*/}
            <Dialog open={!!target} onOpenChange={(o) => { if (!o) close(); }}>
                <DialogContent className="max-w-md">
                    <DialogTitle className="flex items-center gap-2 text-base font-black text-status-error">
                        <ShieldAlert className="w-5 h-5" />
                        {t("settings.agent_reset.confirm_title", { id: target?.id ?? "" })}
                    </DialogTitle>
                    <DialogDescription className="text-xs leading-relaxed">
                        {t("settings.agent_reset.confirm_body")}
                    </DialogDescription>

                    {!view ? (
                        <>
                            <div className="mt-1 grid grid-cols-2 gap-2 text-xs">
                                <div className="rounded-xl border border-status-error/30 bg-status-error/5 p-2">
                                    <p className="font-black text-status-error">{t("settings.agent_reset.wipe_label")}</p>
                                    <p className="text-muted-foreground">
                                        {t(scrub.checked ? "settings.agent_reset.scrub.wipe_list" : "settings.agent_reset.wipe_list")}
                                    </p>
                                </div>
                                <div className="rounded-xl border border-status-success/30 bg-status-success/5 p-2">
                                    <p className="font-black text-status-success">{t("settings.agent_reset.keep_label")}</p>
                                    <p className="text-muted-foreground">
                                        {t(scrub.checked ? "settings.agent_reset.scrub.keep_list" : "settings.agent_reset.keep_list")}
                                    </p>
                                </div>
                            </div>
                            <BackupRetentionNotice variant="reset" agentId={target?.id ?? ""} />
                            <AgentResetScrubOption value={scrub} onChange={setScrub} disabled={busy} />
                            <label className="mt-2 block text-xs font-bold text-foreground">
                                {t("settings.agent_reset.type_to_confirm", { id: target?.id ?? "" })}
                            </label>
                            <input
                                value={typed}
                                onChange={(e) => setTyped(e.target.value)}
                                placeholder={target?.id ?? ""}
                                className="w-full rounded-xl border border-input bg-muted/30 px-3 py-2 font-mono text-sm outline-none focus:ring-2 focus:ring-status-error/40"
                            />
                            <input
                                type="password"
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                                placeholder={t("settings.agent_reset.password_placeholder")}
                                className="w-full rounded-xl border border-input bg-muted/30 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-status-error/40"
                            />
                            <div className="mt-1 flex justify-end gap-2">
                                <button onClick={close} disabled={busy} className="rounded-xl px-4 py-2 text-xs font-bold text-muted-foreground hover:text-foreground">
                                    {t("settings.agent_reset.cancel")}
                                </button>
                                <button
                                    onClick={doReset}
                                    disabled={!canSubmit}
                                    className={cn(
                                        "flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-black uppercase tracking-widest transition-all",
                                        canSubmit ? "bg-destructive text-white hover:opacity-90" : "bg-muted text-muted-foreground cursor-not-allowed",
                                    )}
                                >
                                    {busy ? <Loader2 size={14} className="animate-spin" /> : <Eraser size={14} />}
                                    {t("settings.agent_reset.confirm_reset")}
                                </button>
                            </div>
                        </>
                    ) : (
                        renderResult(view)
                    )}
                </DialogContent>
            </Dialog>
        </div>
    );
}
