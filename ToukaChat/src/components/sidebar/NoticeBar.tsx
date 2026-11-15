import { AlertTriangle, Info } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { MemoryNotice } from "@/lib/SystemStatusContext";
import { cn } from "@/lib/utils";

/** 等級 → 樣式：資訊級平靜、不搶焦點；警告級與降級橫幅同樣醒目，並以 alert 宣告。 */
const LEVEL_STYLES = {
  info: { role: "status", Icon: Info, className: "bg-muted text-foreground border-b border-border" },
  warning: { role: "alert", Icon: AlertTriangle, className: "bg-destructive text-destructive-foreground" },
} as const;

/**
 * 一個代碼對一個翻譯 key；依判斷依據（`basis`）分句子的代碼給一張表，表裡沒有的依據用 `fallback`
 * （寧可顯示通用句子，也不讓警告沒有內容）。
 */
export type NoticeMessageKey = string | { readonly byBasis: ReadonlyMap<string, string>; readonly fallback: string };

interface NoticeBarProps {
  notices?: readonly MemoryNotice[];
  /** 提示代碼 → 翻譯 key（新提示只在呼叫端的表加一行並補翻譯）。不認得的代碼不顯示。 */
  messageKeys: ReadonlyMap<string, NoticeMessageKey>;
  testId: string;
}

function messageKeyFor(entry: NoticeMessageKey, notice: MemoryNotice): string {
  if (typeof entry === "string") return entry;
  return entry.byBasis.get(notice.basis ?? "") ?? entry.fallback;
}

/** 通用提示列：記憶提示與系統提示共用；每個認得的代碼一列，文字由翻譯給。 */
export function NoticeBar({ notices = [], messageKeys, testId }: NoticeBarProps) {
  const { t } = useTranslation();
  const shown = notices.filter(notice => messageKeys.has(notice.code));
  if (shown.length === 0) return null;
  return (
    <>
      {shown.map(notice => {
        const style = LEVEL_STYLES[notice.level];
        const entry = messageKeys.get(notice.code);
        return (
          <div
            key={notice.code}
            role={style.role}
            data-testid={testId}
            data-level={notice.level}
            className={cn("w-full text-xs px-4 py-2 flex items-start gap-2 shrink-0", style.className)}
          >
            <style.Icon size={14} className="shrink-0 mt-0.5" />
            <span className="min-w-0 whitespace-pre-wrap">
              {t(entry ? messageKeyFor(entry, notice) : "", {
                count: notice.count ?? 0,
                configured_model: notice.configured_model ?? "",
                index_model: notice.index_model ?? "",
                index_dim: notice.index_dim ?? "",
              })}
            </span>
          </div>
        );
      })}
    </>
  );
}
