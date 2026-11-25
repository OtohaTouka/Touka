import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import type { LocalFolderEntry, LocalRevokeRequest } from "@/lib/types";

const PREFIX = "settings.local_helper_tab.";

interface LocalHelperFolderRowProps {
    folder: LocalFolderEntry;
    /** 小幫手被看見時不能在這裡改（ADR-0330）。 */
    canChange: boolean;
    disabled: boolean;
    onAsk: (change: LocalRevokeRequest["change"]) => void;
}

/**
 * 開放名單的一列：名稱、權限、暫停中、完整路徑；可以改的才有「改成只能讀」，每一列都有「拿掉」
 * （ADR-0330 2b，只能縮小）。
 */
export function LocalHelperFolderRow({ folder, canChange, disabled, onAsk }: LocalHelperFolderRowProps) {
    const { t } = useTranslation();
    return (
        <li className="bg-background border border-input rounded-xl py-2.5 px-4 space-y-1">
            <p className="text-sm text-foreground">
                <span className="font-bold">{folder.name}</span>{" "}
                <span className="text-muted-foreground">
                    {t(`${PREFIX}${folder.writable ? "folders_writable" : "folders_read_only"}`)}
                </span>
                {!folder.usable && <span className="text-destructive"> {t(`${PREFIX}folders_paused`)}</span>}
            </p>
            <p className="text-xs text-muted-foreground break-all select-all">{folder.path}</p>
            {canChange && (
                <div className="flex gap-2">
                    {folder.writable && (
                        <Button
                            variant="outline"
                            size="sm"
                            disabled={disabled}
                            aria-label={t(`${PREFIX}folders_make_read_only_label`, { name: folder.name })}
                            onClick={() => onAsk("read_only")}
                        >
                            {t(`${PREFIX}folders_make_read_only`)}
                        </Button>
                    )}
                    <Button
                        variant="outline"
                        size="sm"
                        disabled={disabled}
                        aria-label={t(`${PREFIX}folders_remove_label`, { name: folder.name })}
                        onClick={() => onAsk("remove")}
                    >
                        {t(`${PREFIX}folders_remove`)}
                    </Button>
                </div>
            )}
        </li>
    );
}
