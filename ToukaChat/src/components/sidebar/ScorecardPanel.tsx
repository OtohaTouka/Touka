"use client";

import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Trophy, Play, Loader2, Download, ChevronRight, ChevronDown, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useScorecard } from "@/hooks/useScorecard";
import { useDebugReset } from "@/hooks/useDebugReset";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { useModels } from "@/lib/providers/ModelProvider";
import { BackupRetentionNotice } from "@/components/common/BackupRetentionNotice";

/**
 * 模型計分卡面板（Debug Center 第 3 子分頁）。
 *
 * 選模型＋預算 → 確認花費 → 啟動評分（背景 + SSE 即時進度）→ 顯示成績單＋下載。
 * ⚠️ 啟動會以真實模型執行並花費 API 費用，故先經 confirmToast 取得確認。
 */
export function ScorecardPanel() {
    const { t } = useTranslation();
    const { confirmToast } = useConfirmToast();
    const { availableModels } = useModels();
    const { status, progress, scorecard, error, run } = useScorecard();
    const { status: resetStatus, reset } = useDebugReset();
    const [model, setModel] = useState("");
    const [budget, setBudget] = useState("0.50");
    const [cleanSlate, setCleanSlate] = useState(false);
    const [expanded, setExpanded] = useState<Set<string>>(new Set());

    const running = status === "running";
    const budgetNum = parseFloat(budget);
    // 不硬編碼預設模型：未輸入時自動採用第一個可用（能跑的）模型。
    const fallbackModel = availableModels[0]?.id ?? "";
    const effectiveModel = model.trim() || fallbackModel;
    const canRun = !running && !!effectiveModel && budgetNum > 0;

    const handleRun = () => {
        if (!canRun) return;
        confirmToast(
            t("scorecard.confirm_cost", { budget: budgetNum.toFixed(2) }),
            () => { void run(effectiveModel, budgetNum, undefined, cleanSlate); },
        );
    };

    const resetting = resetStatus === "running";
    const handleReset = () => {
        if (resetting) return;
        // 破壞性、不可復原 → 強警語二次確認；後端再以安全閘硬鎖 DebugAgent＋備份優先。
        confirmToast(t("scorecard.reset_confirm"), () => {
            void reset().then((report) => {
                if (!report) {
                    toast.error(t("scorecard.reset_error", { error: t("scorecard.error") }));
                } else if (report.clean) {
                    toast.success(t("scorecard.reset_success"));
                } else {
                    toast.warning(t("scorecard.reset_dirty"));
                }
            });
        });
    };

    const toggleExpand = (id: string) => {
        setExpanded((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id); else next.add(id);
            return next;
        });
    };

    const handleDownload = () => {
        if (!scorecard) return;
        const blob = new Blob([JSON.stringify(scorecard, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `scorecard-${scorecard.model}.json`;
        link.click();
        URL.revokeObjectURL(url);
    };

    const done = progress.length;
    const total = progress[0]?.total ?? 0;
    const spent = progress.length ? (progress[progress.length - 1].spent_usd ?? 0) : 0;

    return (
        <div className="space-y-4">
            <p className="text-xs text-muted-foreground leading-relaxed">{t("scorecard.description")}</p>

            <div className="space-y-1.5">
                <Label htmlFor="scorecard-model" className="text-xs font-bold">{t("scorecard.model_label")}</Label>
                <Input
                    id="scorecard-model"
                    list="scorecard-models"
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder={availableModels[0]?.id ?? t("scorecard.model_placeholder")}
                    className="h-8 text-xs bg-muted/20"
                />
                <datalist id="scorecard-models">
                    {availableModels.map((m) => <option key={m.id} value={m.id} />)}
                </datalist>
                {availableModels.length === 0 && (
                    <p className="text-xs text-muted-foreground">{t("scorecard.empty_models")}</p>
                )}
            </div>

            <div className="space-y-1.5">
                <Label htmlFor="scorecard-budget" className="text-xs font-bold">{t("scorecard.budget_label")}</Label>
                <Input
                    id="scorecard-budget"
                    type="number"
                    step="0.05"
                    min="0"
                    value={budget}
                    onChange={(e) => setBudget(e.target.value)}
                    className="h-8 text-xs bg-muted/20"
                />
            </div>

            <label htmlFor="scorecard-clean" className="flex items-start gap-2 cursor-pointer">
                <input
                    id="scorecard-clean"
                    type="checkbox"
                    checked={cleanSlate}
                    onChange={(e) => setCleanSlate(e.target.checked)}
                    className="mt-0.5 accent-primary"
                />
                <span className="text-xs">
                    <span className="font-bold">{t("scorecard.clean_slate_label")}</span>
                    <span className="block text-muted-foreground leading-relaxed">{t("scorecard.clean_slate_hint")}</span>
                </span>
            </label>

            <Button size="sm" className="w-full h-8 font-bold" onClick={handleRun} disabled={!canRun}>
                {running ? <Loader2 className="h-3.5 w-3.5 mr-2 animate-spin" /> : <Play className="h-3.5 w-3.5 mr-2" />}
                {running ? t("scorecard.running") : t("scorecard.run")}
            </Button>

            {error && <p className="text-xs text-destructive">{t("scorecard.error")}: {error}</p>}

            {running && total > 0 && (
                <p className="text-xs text-muted-foreground tabular-nums">
                    {t("scorecard.progress", { done, total, spent: spent.toFixed(4) })}
                </p>
            )}

            {scorecard && (
                <div className="space-y-2 rounded-lg border border-border bg-muted/20 p-3">
                    <div className="flex items-center gap-2 text-xs font-bold">
                        <Trophy className="h-3.5 w-3.5 text-primary shrink-0" />
                        <span>{t("scorecard.summary", {
                            passed: scorecard.passed,
                            total: scorecard.total,
                            tokens: scorecard.total_tokens,
                            spent: scorecard.spent_usd.toFixed(6),
                        })}</span>
                    </div>
                    <div className="overflow-x-auto">
                        <table className="w-full text-xs">
                            <thead>
                                <tr className="text-left text-muted-foreground">
                                    <th className="py-1 pr-2 font-medium">{t("scorecard.col_scenario")}</th>
                                    <th className="py-1 pr-2 font-medium">{t("scorecard.col_result")}</th>
                                    <th className="py-1 pr-2 font-medium">{t("scorecard.col_tokens")}</th>
                                    <th className="py-1 font-medium">{t("scorecard.col_cost")}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {scorecard.results.map((r) => {
                                    const isOpen = expanded.has(r.scenario);
                                    return (
                                        <Fragment key={r.scenario}>
                                            <tr
                                                className="border-t border-border/50 cursor-pointer hover:bg-muted/30"
                                                onClick={() => toggleExpand(r.scenario)}
                                            >
                                                <td className="py-1 pr-2 font-mono">
                                                    <span className="inline-flex items-center gap-1">
                                                        {isOpen
                                                            ? <ChevronDown className="h-3 w-3 shrink-0" />
                                                            : <ChevronRight className="h-3 w-3 shrink-0" />}
                                                        {r.scenario}
                                                    </span>
                                                </td>
                                                <td className="py-1 pr-2">
                                                    {r.skipped
                                                        ? t("scorecard.status_skipped")
                                                        : r.pass
                                                            ? t("scorecard.status_pass")
                                                            : t("scorecard.status_fail")}
                                                </td>
                                                <td className="py-1 pr-2 tabular-nums">{r.estimated ? "~" : ""}{r.tokens}</td>
                                                <td className="py-1 tabular-nums">{r.estimated ? "~" : ""}{r.cost_usd.toFixed(6)}</td>
                                            </tr>
                                            {isOpen && (
                                                <tr className="bg-muted/10">
                                                    <td colSpan={4} className="py-2 px-2 space-y-1.5">
                                                        {r.detail && r.detail !== "ok" && (
                                                            <div className="text-muted-foreground">{r.detail}</div>
                                                        )}
                                                        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-muted-foreground tabular-nums">
                                                            {typeof r.react_turns === "number" && r.react_turns > 0 && (
                                                                <span>{t("scorecard.react_turns", { n: r.react_turns })}</span>
                                                            )}
                                                            {r.finish_reason && (
                                                                <span>{t("scorecard.finish_label", { reason: r.finish_reason })}</span>
                                                            )}
                                                            {r.early_stop && (
                                                                <span className="text-foreground font-bold">{t("scorecard.early_stopped")}</span>
                                                            )}
                                                            {(r.artifact_valid === true || r.artifact_valid === false) && (
                                                                <span
                                                                    className={r.artifact_valid ? "text-foreground font-bold" : "text-destructive font-bold"}
                                                                    title={r.artifact_detail || ""}
                                                                >
                                                                    {r.artifact_valid ? t("scorecard.artifact_valid") : t("scorecard.artifact_invalid")}
                                                                </span>
                                                            )}
                                                        </div>
                                                        <div className="font-bold">{t("scorecard.trace_title")}</div>
                                                        {r.tool_trace && r.tool_trace.length > 0 ? (
                                                            <ul className="space-y-1">
                                                                {r.tool_trace.map((tr, i) => (
                                                                    <li key={`${tr.name}-${i}`} className="rounded bg-muted/30 p-1.5 break-all">
                                                                        <div className="font-mono font-bold">
                                                                            {tr.name}{tr.status ? ` · ${tr.status}` : ""}
                                                                        </div>
                                                                        {tr.args && (
                                                                            <div><span className="text-muted-foreground">args: </span>{tr.args}</div>
                                                                        )}
                                                                        {tr.result && (
                                                                            <div><span className="text-muted-foreground">result: </span>{tr.result}</div>
                                                                        )}
                                                                    </li>
                                                                ))}
                                                            </ul>
                                                        ) : (
                                                            <div className="text-muted-foreground italic">{t("scorecard.trace_none")}</div>
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
                    {scorecard.results.some((r) => r.estimated) && (
                        <p className="text-xs text-muted-foreground">{t("scorecard.estimated_note")}</p>
                    )}
                    <Button size="sm" variant="outline" className="h-7 text-xs" onClick={handleDownload}>
                        <Download className="h-3 w-3 mr-1.5" /> {t("scorecard.download")}
                    </Button>
                </div>
            )}

            <div className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3">
                <p className="text-xs font-bold text-destructive">{t("scorecard.reset_title")}</p>
                <p className="text-xs text-muted-foreground leading-relaxed">{t("scorecard.reset_hint")}</p>
                <BackupRetentionNotice variant="debug" agentId="DebugAgent" />
                <Button
                    size="sm"
                    variant="destructive"
                    className="w-full h-8 font-bold"
                    onClick={handleReset}
                    disabled={resetting}
                >
                    {resetting
                        ? <Loader2 className="h-3.5 w-3.5 mr-2 animate-spin" />
                        : <Trash2 className="h-3.5 w-3.5 mr-2" />}
                    {resetting ? t("scorecard.reset_running") : t("scorecard.reset_button")}
                </Button>
            </div>
        </div>
    );
}
