"use client";

import { useTranslation } from "react-i18next";
import type { RoundInterruption } from "@/lib/chat/interjections";
import type { Interjection } from "@/lib/types";

interface InterjectionMarkerProps {
  interjections: Interjection[];
  /** ADR-0249 第 4b 段：被 Ctrl+Enter 打斷的那幾輪（第 N 步）。 */
  interruptions?: RoundInterruption[];
}

/** ADR-0249：工具時間軸上標出「你的補充在第 N 步送達」與「這一輪被你打斷」，照步數排。 */
export function InterjectionMarker({ interjections, interruptions = [] }: InterjectionMarkerProps) {
  const { t } = useTranslation();
  const rows = [
    ...interruptions.map((r, idx) => ({
      key: `interrupted-${idx}`, step: r.step, order: 0, text: "",
      label: t("chat.interjection.round_interrupted_at_step", { step: r.step }),
    })),
    ...interjections
      .filter(i => i.status === "delivered" && typeof i.step === "number")
      .map(i => ({
        key: i.id, step: i.step as number, order: 1, text: i.text,
        label: t("chat.interjection.delivered_at_step", { step: i.step }),
      })),
  ].sort((a, b) => a.step - b.step || a.order - b.order);
  if (rows.length === 0) return null;
  return (
    <ul aria-label={t("chat.interjection.marker_label")}
      className="flex flex-col gap-1 my-2 border-l-2 border-primary/20 pl-3 text-xs text-muted-foreground">
      {rows.map(row => (
        <li key={row.key} className="flex flex-col">
          <span className="font-bold">{row.label}</span>
          {row.text && <span className="whitespace-pre-wrap break-words">{row.text}</span>}
        </li>
      ))}
    </ul>
  );
}
