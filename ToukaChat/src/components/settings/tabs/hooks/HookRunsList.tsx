import { useTranslation } from "react-i18next";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatToTaipeiTime } from "@/lib/time_utils";
import type { HookRun } from "@/lib/types";

interface HookRunsListProps {
    runs: HookRun[];
    onRefresh: () => void;
}

/** 最近的執行紀錄（新的在前；只在伺服器記憶體，重啟後清空）。 */
export function HookRunsList({ runs, onRefresh }: HookRunsListProps) {
    const { t } = useTranslation();
    return (
        <section className="space-y-3">
            <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                    <h5 className="text-sm font-semibold text-foreground">{t("settings.hooks_tab.runs.title")}</h5>
                    <p className="text-xs text-muted-foreground">{t("settings.hooks_tab.runs.hint")}</p>
                </div>
                <Button variant="outline" size="sm" onClick={onRefresh}>
                    <RefreshCw className="h-3.5 w-3.5" /> {t("settings.hooks_tab.runs.refresh")}
                </Button>
            </div>
            {runs.length === 0 ? (
                <p className="text-xs text-muted-foreground">{t("settings.hooks_tab.runs.empty")}</p>
            ) : (
                <ul className="divide-y divide-border rounded-lg border border-border text-xs">
                    {runs.map((run, index) => (
                        <li key={`${run.at}-${index}`} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 p-2">
                            <span className="text-muted-foreground">{formatToTaipeiTime(run.at * 1000)}</span>
                            <span className="font-mono">{run.event}</span>
                            <span className="font-medium">
                                {run.incognito ? t("settings.hooks_tab.runs.incognito") : run.hook}
                            </span>
                            {!run.incognito && (run.hook_type === "command" || run.hook_type === "http") && (
                                <span className="text-muted-foreground">{t(`settings.hooks_tab.types.${run.hook_type}`)}</span>
                            )}
                            <span>{t(`settings.hooks_tab.outcomes.${run.outcome}`)}</span>
                            {run.exit_code != null && (
                                <span className="font-mono">{t("settings.hooks_tab.runs.exit_code", { code: run.exit_code })}</span>
                            )}
                            {run.http_status != null && (
                                <span className="font-mono">{t("settings.hooks_tab.runs.http_status", { status: run.http_status })}</span>
                            )}
                            <span className="text-muted-foreground">
                                {t("settings.hooks_tab.runs.duration", { ms: run.duration_ms })}
                            </span>
                            {run.detail && <span className="w-full break-all text-muted-foreground">{run.detail}</span>}
                        </li>
                    ))}
                </ul>
            )}
        </section>
    );
}
