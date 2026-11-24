import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { useLocalFolderRevoke, type RevokeOutcome } from "@/hooks/settings/useLocalFolderRevoke";
import { useLocalHelperFolders, type LocalHelperFolders as Folders } from "@/hooks/settings/useLocalHelperFolders";
import type { ErrorCode } from "@/lib/error_codes";
import type { LocalFolderEntry, LocalFolderList, LocalHelperStatus, LocalRevokeRequest } from "@/lib/types";
import { LocalHelperFolderRow } from "./LocalHelperFolderRow";

const PREFIX = "settings.local_helper_tab.";
const RESULT_PREFIX = `${PREFIX}revoke_result.`;

// 清單以外的狀態各說一句；只有 ok 才有清單（ADR-0330）
const LIST_STATE_MESSAGES: Record<Exclude<LocalFolderList["state"], "ok">, string> = {
    offline: "folders_offline",
    waiting: "folders_waiting",
    stale: "folders_stale",
};

// 拿掉或改成只能讀失敗時的說法（ADR-0330 2b）；不認得的一律說「不確定有沒有改」
const REVOKE_CODE_MESSAGES: Record<string, string> = {
    not_found: "not_found",
    helper_exposed: "helper_exposed",
    bad_signature: "bad_signature",
    needs_new_pairing: "needs_new_pairing",
    io_error: "io_error",
    helper_offline: "helper_offline",
    helper_busy: "helper_busy",
    timed_out: "not_sent",
    pairing_changed: "not_sent",
};

const REVOKE_DENIED_MESSAGES: Partial<Record<ErrorCode, string>> = {
    LOCAL_HELPER_NOT_PAIRED_HUMAN: "not_paired_human",
    LOCAL_HELPER_NOT_PAIRED: "not_paired",
};

interface Message {
    key: string;
    values?: Record<string, string>;
}

function notice(folders: Folders): string | null {
    if (folders.kind === "denied") return "folders_denied";
    if (folders.kind === "failed") return "folders_failed";
    if (folders.kind === "loaded" && folders.list.state !== "ok") return LIST_STATE_MESSAGES[folders.list.state];
    return null;
}

function revokeMessage(outcome: RevokeOutcome): Message {
    if (outcome.kind === "denied") {
        return { key: (outcome.code && REVOKE_DENIED_MESSAGES[outcome.code]) || "denied" };
    }
    if (outcome.kind === "failed") return { key: "unknown" };
    const { status, code, name, removed } = outcome.result;
    if (status === "done") return { key: removed ? "removed" : "read_only", values: { name: name ?? "" } };
    return { key: (code && REVOKE_CODE_MESSAGES[code]) || "unknown" };
}

interface Asking {
    folder: LocalFolderEntry;
    change: LocalRevokeRequest["change"];
}

interface LocalHelperFoldersProps {
    status: LocalHelperStatus;
}

/**
 * 小幫手開放了哪些資料夾（ADR-0330）：完整路徑只給配對的人看，以小幫手主視窗為準。
 * 這裡只能縮小（拿掉、改成只能讀，2b），按了先確認；加回或改回可以改要到主視窗。
 * 配對了、小幫手開著時才顯示。
 */
export function LocalHelperFolders({ status }: LocalHelperFoldersProps) {
    const { t } = useTranslation();
    const visible = status.paired && status.online;
    const [refresh, setRefresh] = useState(0);
    const [asking, setAsking] = useState<Asking | null>(null);
    const folders = useLocalHelperFolders(visible, refresh);
    const { pending, outcome, revoke } = useLocalFolderRevoke();

    if (!visible) return null;

    const message = notice(folders);
    const list = folders.kind === "loaded" && folders.list.state === "ok" ? folders.list : null;
    const shown = list?.folders ?? [];
    const result = outcome ? revokeMessage(outcome) : null;

    const confirm = async (ask: Asking) => {
        setAsking(null);
        const next = await revoke({ name: ask.folder.name, path: ask.folder.path }, ask.change);
        if (next.kind === "result" && next.result.status === "done") setRefresh((value) => value + 1);
    };

    return (
        <div className="space-y-2">
            <p className="text-sm font-bold text-foreground">{t(`${PREFIX}folders_heading`)}</p>
            <p className="text-xs text-muted-foreground">{t(`${PREFIX}folders_help`)}</p>
            {message && <p className="text-sm text-muted-foreground">{t(`${PREFIX}${message}`)}</p>}
            {list?.exposed && <p className="text-sm text-destructive">{t(`${PREFIX}folders_exposed`)}</p>}
            {list && shown.length === 0 && <p className="text-sm text-muted-foreground">{t(`${PREFIX}folders_empty`)}</p>}
            {list && shown.length > 0 && (
                <ul className="space-y-2">
                    {shown.map((folder, index) => (
                        <LocalHelperFolderRow
                            key={`${index}:${folder.path}`}
                            folder={folder}
                            canChange={!list.exposed}
                            disabled={pending}
                            onAsk={(change) => setAsking({ folder, change })}
                        />
                    ))}
                </ul>
            )}
            {list && list.omitted > 0 && (
                <p className="text-xs text-muted-foreground">{t(`${PREFIX}folders_omitted`, { count: list.omitted })}</p>
            )}
            {asking && (
                <div className="space-y-2">
                    <p className="text-sm text-foreground">
                        {t(
                            `${PREFIX}${asking.change === "remove" ? "folders_confirm_remove" : "folders_confirm_read_only"}`,
                            { name: asking.folder.name },
                        )}
                    </p>
                    <div className="flex gap-2">
                        <Button size="sm" disabled={pending} onClick={() => void confirm(asking)}>
                            {t(`${PREFIX}confirm`)}
                        </Button>
                        <Button variant="outline" size="sm" onClick={() => setAsking(null)}>
                            {t(`${PREFIX}cancel`)}
                        </Button>
                    </div>
                </div>
            )}
            {!pending && result && (
                <p className="text-sm text-foreground">{t(`${RESULT_PREFIX}${result.key}`, result.values)}</p>
            )}
        </div>
    );
}
