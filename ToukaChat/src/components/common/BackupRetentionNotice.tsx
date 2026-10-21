"use client";

import { useTranslation } from "react-i18next";
import { Archive } from "lucide-react";

/** 「還有備份」提醒用在哪個畫面：重置卡（清除連備份完成後另一版）、刪除靈魂、DebugAgent 重置。 */
type BackupNoticeVariant = "reset" | "reset_scrubbed" | "delete" | "debug";

// 各畫面要列的句子（`backup_notice.*`）；新畫面只加一列，不改元件。
const LEAD_KEY: Record<BackupNoticeVariant, string> = {
    reset: "still_there", reset_scrubbed: "still_there_scrubbed", delete: "still_there_delete", debug: "still_there",
};
const BOTH_WHERE = ["where_snapshots", "where_upgrade"] as const;
const WHERE_KEYS: Record<BackupNoticeVariant, readonly string[]> = {
    reset: BOTH_WHERE, reset_scrubbed: ["where_snapshots"], delete: BOTH_WHERE, debug: BOTH_WHERE,
};
const PURGE_KEYS: Record<BackupNoticeVariant, readonly string[]> = {
    reset: ["purge_scrub", "purge_snapshot", "purge_upgrade"],
    reset_scrubbed: ["purge_snapshot"],
    delete: ["purge_scrub_delete", "purge_snapshot", "purge_upgrade"],
    debug: ["purge_snapshot", "purge_upgrade"],
};
const NOTE_KEYS: Record<BackupNoticeVariant, readonly string[]> = {
    reset: ["restore_effect", "not_context_snapshot"],
    reset_scrubbed: ["restore_effect"],
    delete: ["no_auto_snapshot", "restore_effect"],
    debug: ["restore_effect", "not_context_snapshot"],
};

/**
 * ADR-0270 補記（hist-bak a）：刪除或重置後，系統快照與升級備份裡仍有這個代理人的資料。
 * 位置是固定文字（伺服器不回傳路徑）；在哪裡、怎麼徹底刪收在可展開的區塊裡。
 */
export function BackupRetentionNotice({ variant, agentId }: { variant: BackupNoticeVariant; agentId: string }) {
    const { t } = useTranslation();
    const line = (key: string) => <li key={key}>{t(`backup_notice.${key}`, { id: agentId })}</li>;

    return (
        <div className="rounded-xl border border-status-warning/30 bg-status-warning/5 p-2 text-xs text-muted-foreground">
            <p className="flex items-start gap-1 font-bold text-status-warning">
                <Archive size={12} className="mt-0.5 shrink-0" />
                <span>{t(`backup_notice.${LEAD_KEY[variant]}`, { id: agentId })}</span>
            </p>
            <details className="mt-1">
                <summary className="cursor-pointer font-bold text-foreground">{t("backup_notice.summary")}</summary>
                <ul className="mt-1 list-disc space-y-0.5 pl-4">{WHERE_KEYS[variant].map(line)}</ul>
                <p className="mt-1 font-bold text-foreground">{t("backup_notice.purge_title")}</p>
                <ul className="list-disc space-y-0.5 pl-4">{PURGE_KEYS[variant].map(line)}</ul>
                <ul className="mt-1 space-y-0.5">{NOTE_KEYS[variant].map(line)}</ul>
            </details>
        </div>
    );
}
