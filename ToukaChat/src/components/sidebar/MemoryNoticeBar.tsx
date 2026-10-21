import type { MemoryNotice } from "@/lib/SystemStatusContext";
import { NoticeBar } from "./NoticeBar";

/** 提示代碼 → 翻譯 key（新提示只在這裡加一行並補翻譯）。不認得的代碼不顯示。 */
const NOTICE_MESSAGE_KEYS = new Map<string, string>([
  ["unmigrated_legacy_facts", "sidebar.memory_notice.unmigrated_legacy_facts"],
  ["memory_upgrade_no_space", "sidebar.memory_notice.memory_upgrade_no_space"],
  ["memory_upgrade_locked", "sidebar.memory_notice.memory_upgrade_locked"],
  ["memory_store_unreadable", "sidebar.memory_notice.memory_store_unreadable"],
  ["memory_upgrade_failed", "sidebar.memory_notice.memory_upgrade_failed"],
  ["memory_new_store_beside_backups", "sidebar.memory_notice.memory_new_store_beside_backups"],
]);

interface MemoryNoticeBarProps {
  notices?: readonly MemoryNotice[];
}

/**
 * 記憶提示列：顯示 /v1/memory 的 memory_notices（例如舊記憶檔還沒搬進記憶庫）。
 * 與降級橫幅分開：這些提示只給人看、不進模型的 prompt（記憶庫升級或讀取失敗也走這裡）。
 */
export function MemoryNoticeBar({ notices = [] }: MemoryNoticeBarProps) {
  return <NoticeBar notices={notices} messageKeys={NOTICE_MESSAGE_KEYS} testId="memory-notice" />;
}
