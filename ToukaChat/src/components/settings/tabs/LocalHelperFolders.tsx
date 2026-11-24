import { useTranslation } from "react-i18next";
import { useLocalHelperFolders, type LocalHelperFolders as Folders } from "@/hooks/settings/useLocalHelperFolders";
import type { LocalFolderList, LocalHelperStatus } from "@/lib/types";

const PREFIX = "settings.local_helper_tab.";

// 清單以外的狀態各說一句；只有 ok 才有清單（ADR-0330）
const LIST_STATE_MESSAGES: Record<Exclude<LocalFolderList["state"], "ok">, string> = {
    offline: "folders_offline",
    waiting: "folders_waiting",
    stale: "folders_stale",
};

function notice(folders: Folders): string | null {
    if (folders.kind === "denied") return "folders_denied";
    if (folders.kind === "failed") return "folders_failed";
    if (folders.kind === "loaded" && folders.list.state !== "ok") return LIST_STATE_MESSAGES[folders.list.state];
    return null;
}

interface LocalHelperFoldersProps {
    status: LocalHelperStatus;
}

/**
 * 小幫手開放了哪些資料夾（ADR-0330）：完整路徑只給配對的人看，只能看、不能在這裡改，
 * 以小幫手主視窗為準。配對了、小幫手開著時才顯示。
 */
export function LocalHelperFolders({ status }: LocalHelperFoldersProps) {
    const { t } = useTranslation();
    const visible = status.paired && status.online;
    const folders = useLocalHelperFolders(visible);

    if (!visible) return null;

    const message = notice(folders);
    const list = folders.kind === "loaded" && folders.list.state === "ok" ? folders.list : null;

    return (
        <div className="space-y-2">
            <p className="text-sm font-bold text-foreground">{t(`${PREFIX}folders_heading`)}</p>
            <p className="text-xs text-muted-foreground">{t(`${PREFIX}folders_help`)}</p>
            {message && <p className="text-sm text-muted-foreground">{t(`${PREFIX}${message}`)}</p>}
            {list?.exposed && <p className="text-sm text-destructive">{t(`${PREFIX}folders_exposed`)}</p>}
            {list && (list.folders ?? []).length === 0 && (
                <p className="text-sm text-muted-foreground">{t(`${PREFIX}folders_empty`)}</p>
            )}
            {list && (list.folders ?? []).length > 0 && (
                <ul className="space-y-2">
                    {(list.folders ?? []).map((folder, index) => (
                        <li key={`${index}:${folder.path}`} className="bg-background border border-input rounded-xl py-2.5 px-4 space-y-1">
                            <p className="text-sm text-foreground">
                                <span className="font-bold">{folder.name}</span>{" "}
                                <span className="text-muted-foreground">
                                    {t(`${PREFIX}${folder.writable ? "folders_writable" : "folders_read_only"}`)}
                                </span>
                                {!folder.usable && (
                                    <span className="text-destructive"> {t(`${PREFIX}folders_paused`)}</span>
                                )}
                            </p>
                            <p className="text-xs text-muted-foreground break-all select-all">{folder.path}</p>
                        </li>
                    ))}
                </ul>
            )}
            {list && list.omitted > 0 && (
                <p className="text-xs text-muted-foreground">{t(`${PREFIX}folders_omitted`, { count: list.omitted })}</p>
            )}
        </div>
    );
}
