import type { MemoryNotice } from "@/lib/SystemStatusContext";
import { NoticeBar, type NoticeMessageKey } from "./NoticeBar";

const EMBEDDING_MISMATCH_SAMPLE = "sidebar.memory_notice.embedding_model_mismatch_sample";

/** 提示代碼 → 翻譯 key（新提示只在這裡加一行並補翻譯）。不認得的代碼不顯示。 */
const NOTICE_MESSAGE_KEYS = new Map<string, NoticeMessageKey>([
  ["unmigrated_legacy_facts", "sidebar.memory_notice.unmigrated_legacy_facts"],
  ["memory_upgrade_no_space", "sidebar.memory_notice.memory_upgrade_no_space"],
  ["memory_upgrade_locked", "sidebar.memory_notice.memory_upgrade_locked"],
  ["memory_store_unreadable", "sidebar.memory_notice.memory_store_unreadable"],
  ["memory_upgrade_failed", "sidebar.memory_notice.memory_upgrade_failed"],
  ["memory_new_store_beside_backups", "sidebar.memory_notice.memory_new_store_beside_backups"],
  // 嵌入模型對不上（ADR-0315）：知道記憶用的模型名稱／只知道向量長度／長度相同但抽查算出來不同
  ["embedding_model_mismatch", {
    byBasis: new Map([
      ["name", "sidebar.memory_notice.embedding_model_mismatch_name"],
      ["length", "sidebar.memory_notice.embedding_model_mismatch_length"],
      ["sample", EMBEDDING_MISMATCH_SAMPLE],
    ]),
    fallback: EMBEDDING_MISMATCH_SAMPLE,
  }],
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
