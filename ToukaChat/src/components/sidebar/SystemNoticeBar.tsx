import type { SystemNotice } from "@/lib/SystemStatusContext";
import { NoticeBar } from "./NoticeBar";

/** /health 的 system_notices 代碼 → 翻譯 key（新提示只在這裡加一行並補翻譯）。不認得的代碼不顯示。 */
const SYSTEM_NOTICE_KEYS = new Map<string, string>([
  ["autonomy_config_unreadable", "sidebar.system_notice.autonomy_config_unreadable"],
  ["auth_policies_unreadable", "sidebar.system_notice.auth_policies_unreadable"],
]);

interface SystemNoticeBarProps {
  notices?: readonly SystemNotice[];
}

/** 系統提示列（iso-c2 U2h 通用提示管道）：設定讀不到而停掉的功能，寫出原因與恢復方法。 */
export function SystemNoticeBar({ notices = [] }: SystemNoticeBarProps) {
  return <NoticeBar notices={notices} messageKeys={SYSTEM_NOTICE_KEYS} testId="system-notice" />;
}
