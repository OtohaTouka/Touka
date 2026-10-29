import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { AlertTriangle, Loader2, RotateCcw, Save, Webhook } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { useDirtyGuard } from "@/components/settings/SettingsDirtyContext";
import { errorMsg } from "@/lib/error_handler";
import type { HooksConfig, HooksConfigResponse } from "@/lib/types";
import { useHooksSettings, type HookSaveResult } from "@/hooks/settings/useHooksSettings";
import { HookEventSection } from "./hooks/HookEventSection";
import { HookRunsList } from "./hooks/HookRunsList";
import { withGroups } from "./hooks/hook_draft";

type SaveProblem = Exclude<HookSaveResult, { kind: "saved" }>;

/** 設定畫面的「Hooks」分頁（ADR-0292，只給管理員）。 */
export function HooksTab() {
    const { t } = useTranslation();
    const { data, runs, loading, loadFailed, saving, fetchConfig, fetchRuns, saveConfig } = useHooksSettings();
    const [draft, setDraft] = useState<HooksConfig | null>(null);
    const [dirty, setDirty] = useState(false);
    const [problem, setProblem] = useState<SaveProblem | null>(null);
    const [loaded, setLoaded] = useState<HooksConfigResponse | null>(null);
    useDirtyGuard("hooks", dirty);

    // 重新讀到設定（載入、存檔後、按重新載入）就換掉草稿
    if (data !== loaded) {
        setLoaded(data);
        setDraft(data ? data.config : null);
        setDirty(false);
    }

    const edit = (next: HooksConfig) => {
        setDraft(next);
        setDirty(true);
        setProblem(null);
    };

    const handleSave = async () => {
        if (!draft) return;
        const result = await saveConfig(draft);
        if (result.kind === "saved") {
            setProblem(null);
            toast.success(t("settings.hooks_tab.save_success"));
            return;
        }
        setProblem(result);
        if (result.kind === "conflict") toast.error(errorMsg("HOOK_CONFIG_CONFLICT"));
        if (result.kind === "failed") toast.error(t("settings.hooks_tab.errors.save_failed"));
    };

    if (loading && !draft) {
        return (
            <div className="flex items-center justify-center py-12">
                <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
        );
    }
    if (loadFailed || !data || !draft) {
        return <p className="py-12 text-center text-sm text-muted-foreground">{t("settings.hooks_tab.errors.load_failed")}</p>;
    }

    return (
        <div className="animate-in fade-in slide-in-from-bottom-2 space-y-6 duration-300">
            <section className="space-y-1">
                <h4 className="flex items-center gap-2 text-sm font-bold text-foreground">
                    <Webhook className="h-4 w-4 text-primary" /> {t("settings.hooks_tab.title")}
                </h4>
                <p className="max-w-2xl text-xs leading-relaxed text-muted-foreground">{t("settings.hooks_tab.description")}</p>
            </section>

            {data.health.status === "error" && (
                <Notice text={t("settings.hooks_tab.health_error", { detail: data.health.detail ?? "" })} />
            )}

            <div className="flex items-center justify-between rounded-lg border border-border p-4">
                <div className="space-y-0.5">
                    <p className="text-sm font-medium text-foreground">{t("settings.hooks_tab.enable_label")}</p>
                    <p className="text-xs text-muted-foreground">{t("settings.hooks_tab.enable_hint")}</p>
                </div>
                <Switch
                    checked={draft.enabled}
                    onCheckedChange={(enabled) => edit({ ...draft, enabled })}
                    aria-label={t("settings.hooks_tab.enable_label")}
                />
            </div>

            {data.wired_events.map((event) => (
                <HookEventSection
                    key={event}
                    event={event}
                    usesMatcher={data.matcher_events.includes(event)}
                    groups={draft.hooks?.[event] ?? []}
                    onChange={(groups) => edit(withGroups(draft, event, groups))}
                />
            ))}

            {problem && <SaveProblemNotice problem={problem} />}

            <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => { setProblem(null); fetchConfig(); }} disabled={saving}>
                    <RotateCcw className="h-3.5 w-3.5" /> {t("settings.hooks_tab.reload")}
                </Button>
                <Button size="sm" onClick={handleSave} disabled={!dirty || saving}>
                    {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                    {t("settings.hooks_tab.save_button")}
                </Button>
            </div>

            <HookRunsList runs={runs} onRefresh={fetchRuns} />
        </div>
    );
}

function Notice({ text, children }: { text: string; children?: ReactNode }) {
    return (
        <div className="space-y-1 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-xs text-destructive">
            <p className="flex items-center gap-2">
                <AlertTriangle className="h-4 w-4 shrink-0" /> {text}
            </p>
            {children}
        </div>
    );
}

function SaveProblemNotice({ problem }: { problem: SaveProblem }) {
    const { t } = useTranslation();
    if (problem.kind === "path_rejected") {
        return (
            <Notice text={errorMsg("HOOK_PATH_REJECTED")}>
                <ul className="list-disc space-y-0.5 pl-8">
                    {problem.problems.map((p, i) => (
                        <li key={i} className="break-all">
                            {t("settings.hooks_tab.path_problem", {
                                event: p.event, where: p.where, value: p.value,
                                reason: t(`settings.hooks_tab.path_reasons.${p.reason}`, { defaultValue: p.reason }),
                            })}
                        </li>
                    ))}
                </ul>
            </Notice>
        );
    }
    if (problem.kind === "invalid") {
        return <Notice text={t("settings.hooks_tab.errors.invalid_field", { where: problem.where, message: problem.message })} />;
    }
    if (problem.kind === "conflict") return <Notice text={errorMsg("HOOK_CONFIG_CONFLICT")} />;
    return <Notice text={t("settings.hooks_tab.errors.save_failed")} />;
}
