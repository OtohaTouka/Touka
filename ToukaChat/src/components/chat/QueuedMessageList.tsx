"use client";

import { useTranslation } from "react-i18next";
import { Pencil, X } from "lucide-react";
import type { QueuedInterjection } from "@/lib/chat/interjectionStore";

interface QueuedMessageListProps {
  items: QueuedInterjection[];
  onRetract: (item: QueuedInterjection) => void;
  onEdit: (item: QueuedInterjection) => void;
}

/** ADR-0249：AI 工作中排隊的補充（灰色）；可收回，或收回後放回輸入框修改。 */
export function QueuedMessageList({ items, onRetract, onEdit }: QueuedMessageListProps) {
  const { t } = useTranslation();
  if (items.length === 0) return null;
  return (
    <ul aria-label={t("chat.interjection.list_label")} className="px-3 pt-3 flex flex-col gap-1">
      {items.map(item => (
        <li key={item.id} className="flex items-start gap-2 rounded-lg bg-muted/50 px-3 py-1.5 text-xs text-muted-foreground">
          <span className="flex-1 min-w-0 whitespace-pre-wrap break-words">{item.text}</span>
          {item.urgent && <span className="shrink-0 font-bold text-primary">{t("chat.interjection.urgent")}</span>}
          <span className="shrink-0">
            {t(item.status === "undelivered" ? "chat.interjection.after_turn" : "chat.interjection.queued")}
          </span>
          {/* WCAG 2.5.8：點擊區 ≥24px（p-1.5×2 + icon12） */}
          <button type="button" onClick={() => onEdit(item)} aria-label={t("chat.interjection.edit")}
            className="shrink-0 p-1.5 rounded-full hover:text-primary">
            <Pencil size={12} />
          </button>
          <button type="button" onClick={() => onRetract(item)} aria-label={t("chat.interjection.retract")}
            className="shrink-0 p-1.5 rounded-full hover:text-status-error">
            <X size={12} />
          </button>
        </li>
      ))}
    </ul>
  );
}
