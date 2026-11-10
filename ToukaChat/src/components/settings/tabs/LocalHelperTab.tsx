import { useState } from "react";
import { useTranslation } from "react-i18next";
import { HardDrive } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useLocalHelper } from "@/hooks/settings/useLocalHelper";
import { logSSOTError } from "@/lib/error_handler";

type PendingAction = "rotate" | "revoke" | null;

/** 本機小幫手的配對（ADR-0311）。開放哪些資料夾、核准都在小幫手自己的視窗，這裡只管配對。 */
export function LocalHelperTab() {
    const { t } = useTranslation();
    const { status, failed, code, busy, createPairing, revokePairing } = useLocalHelper();
    const [pending, setPending] = useState<PendingAction>(null);

    const run = async (action: () => Promise<boolean>) => {
        setPending(null);
        if (!(await action())) toast.error(t("settings.local_helper_tab.action_failed"));
    };

    const copyCode = async () => {
        if (!code) return;
        try {
            await navigator.clipboard.writeText(code);
            toast.success(t("settings.local_helper_tab.copied"));
        } catch (err) {
            logSSOTError(err, "LocalHelperTab:copy");
            toast.error(t("settings.local_helper_tab.action_failed"));
        }
    };

    const statusText = () => {
        if (!status) return "";
        if (!status.paired) return t("settings.local_helper_tab.status_unpaired");
        if (!status.online) return t("settings.local_helper_tab.status_offline");
        return t("settings.local_helper_tab.status_online", { version: status.helper_version ?? "" });
    };

    return (
        <section className="space-y-4">
            <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                <HardDrive className="w-4 h-4 text-primary" /> {t("settings.local_helper_tab.heading")}
            </h4>
            <p className="text-sm text-muted-foreground">{t("settings.local_helper_tab.intro")}</p>

            {failed && !status ? (
                <p className="text-sm text-destructive">{t("settings.local_helper_tab.load_failed")}</p>
            ) : (
                status && (
                    <div className="bg-muted/20 border border-border rounded-3xl p-6 space-y-4">
                        <p className="text-sm text-foreground">{statusText()}</p>

                        {code && (
                            <div className="space-y-2">
                                <p className="text-sm font-bold text-foreground">{t("settings.local_helper_tab.code_title")}</p>
                                <code className="block break-all text-sm bg-background border border-input rounded-xl py-2.5 px-4">{code}</code>
                                <p className="text-xs text-muted-foreground">{t("settings.local_helper_tab.code_help")}</p>
                                <Button variant="outline" size="sm" onClick={() => void copyCode()}>
                                    {t("settings.local_helper_tab.copy")}
                                </Button>
                            </div>
                        )}

                        {pending ? (
                            <div className="space-y-2">
                                <p className="text-sm text-foreground">
                                    {t(pending === "rotate" ? "settings.local_helper_tab.rotate_confirm" : "settings.local_helper_tab.revoke_confirm")}
                                </p>
                                <div className="flex gap-2">
                                    <Button
                                        size="sm"
                                        disabled={busy}
                                        onClick={() => void run(pending === "rotate" ? createPairing : revokePairing)}
                                    >
                                        {t("settings.local_helper_tab.confirm")}
                                    </Button>
                                    <Button variant="outline" size="sm" onClick={() => setPending(null)}>
                                        {t("settings.local_helper_tab.cancel")}
                                    </Button>
                                </div>
                            </div>
                        ) : (
                            <div className="flex gap-2">
                                {status.paired ? (
                                    <>
                                        <Button size="sm" disabled={busy} onClick={() => setPending("rotate")}>
                                            {t("settings.local_helper_tab.rotate")}
                                        </Button>
                                        <Button variant="outline" size="sm" disabled={busy} onClick={() => setPending("revoke")}>
                                            {t("settings.local_helper_tab.revoke")}
                                        </Button>
                                    </>
                                ) : (
                                    <Button size="sm" disabled={busy} onClick={() => void run(createPairing)}>
                                        {t("settings.local_helper_tab.pair")}
                                    </Button>
                                )}
                            </div>
                        )}
                    </div>
                )
            )}
        </section>
    );
}
