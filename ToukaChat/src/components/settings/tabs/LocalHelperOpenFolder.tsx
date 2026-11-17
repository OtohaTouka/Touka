import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useLocalOpenRequest, type OpenOutcome } from "@/hooks/settings/useLocalOpenRequest";
import type { ErrorCode } from "@/lib/error_codes";
import type { LocalHelperStatus } from "@/lib/types";

const RESULT_PREFIX = "settings.local_helper_tab.open_result.";
const INPUT_ID = "local-helper-open-path";

// 小幫手回的代碼對到哪一句（ADR-0320）；舊版小幫手只會回 bad_request 或 not_supported
const CODE_MESSAGES: Record<string, string> = {
    not_opened: "not_opened",
    no_decision: "no_decision",
    not_openable: "not_openable",
    open_paused: "open_paused",
    bad_signature: "bad_signature",
    needs_new_pairing: "needs_new_pairing",
    bad_request: "too_old",
    not_supported: "too_old",
    result_unknown: "maybe_opened",
    bad_result: "maybe_opened",
    approval_busy: "approval_busy",
    approval_cooldown: "approval_cooldown",
    helper_offline: "helper_offline",
    helper_busy: "helper_busy",
    helper_exposed: "helper_exposed",
    pairing_changed: "pairing_changed",
    timed_out: "timed_out",
    io_error: "io_error",
};

const DENIED_MESSAGES: Partial<Record<ErrorCode, string>> = {
    LOCAL_HELPER_NOT_PAIRED_HUMAN: "not_paired_human",
    LOCAL_HELPER_NOT_PAIRED: "not_paired",
};

interface Message {
    key: string;
    values?: Record<string, string>;
}

function outcomeMessage(outcome: OpenOutcome): Message {
    switch (outcome.kind) {
        case "result": {
            const { status, code, name, writable } = outcome.result;
            if (status === "opened") {
                return {
                    key: writable ? "opened_writable" : "opened_read_only",
                    values: { name: name ?? "" },
                };
            }
            const known = code ? CODE_MESSAGES[code] : undefined;
            return known ? { key: known } : { key: "unknown_code", values: { code: code ?? "" } };
        }
        case "denied":
            return { key: (outcome.code && DENIED_MESSAGES[outcome.code]) || "denied" };
        case "invalid":
            return { key: "invalid_path" };
        case "timeout":
            return { key: "maybe_opened" };
        default:
            return { key: "failed" };
    }
}

interface LocalHelperOpenFolderProps {
    status: LocalHelperStatus;
}

/**
 * 從設定頁請小幫手開放資料夾（ADR-0320）。配對了、小幫手開著時顯示；
 * 等待中或已有結果時，小幫手斷線也繼續顯示，結果才看得到。
 */
export function LocalHelperOpenFolder({ status }: LocalHelperOpenFolderProps) {
    const { t } = useTranslation();
    const { pending, outcome, requestOpen } = useLocalOpenRequest();
    const [path, setPath] = useState("");

    if (!status.paired || (!status.online && !pending && outcome === null)) return null;

    const message = outcome ? outcomeMessage(outcome) : null;

    return (
        <div className="space-y-2">
            <p className="text-sm font-bold text-foreground">{t("settings.local_helper_tab.open_heading")}</p>
            <p className="text-xs text-muted-foreground">{t("settings.local_helper_tab.open_help")}</p>
            <Label htmlFor={INPUT_ID}>{t("settings.local_helper_tab.open_label")}</Label>
            <div className="flex gap-2">
                <Input
                    id={INPUT_ID}
                    value={path}
                    placeholder={t("settings.local_helper_tab.open_placeholder")}
                    onChange={(event) => setPath(event.target.value)}
                />
                <Button size="sm" disabled={pending || !path.trim()} onClick={() => void requestOpen(path)}>
                    {t("settings.local_helper_tab.open_submit")}
                </Button>
            </div>
            {pending && <p className="text-sm text-foreground">{t("settings.local_helper_tab.open_waiting")}</p>}
            {!pending && message && (
                <p className="text-sm text-foreground">{t(`${RESULT_PREFIX}${message.key}`, message.values)}</p>
            )}
        </div>
    );
}
