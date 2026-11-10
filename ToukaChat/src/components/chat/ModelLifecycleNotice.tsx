"use client";

import { AlertTriangle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useModelLifecycleAlerts } from "@/hooks/useModelLifecycleAlerts";
import { formatToTaipeiDate } from "@/lib/time_utils";
import type { ModelLifecycleAlert } from "@/lib/types";

const MESSAGE_KEYS: Record<ModelLifecycleAlert["status"], string> = {
  retiring: "chat.model_lifecycle.retiring",
  overdue: "chat.model_lifecycle.overdue",
  date_unknown: "chat.model_lifecycle.date_unknown",
  retired: "chat.model_lifecycle.retired",
  not_found: "chat.model_lifecycle.not_found",
};
const MS_PER_SECOND = 1000;

/** 在用的模型快下架或已標停用（ADR-0310）。只提醒，不換模型；提示的範圍由後端依身分決定。 */
export function ModelLifecycleNotice() {
  const { t } = useTranslation();
  const alerts = useModelLifecycleAlerts();
  if (alerts.length === 0) return null;
  return (
    <div
      role="status"
      data-testid="model-lifecycle-notice"
      className="m-2 flex items-start gap-2 p-3 bg-status-warning/10 border border-status-warning/20 rounded-xl text-status-warning text-sm"
    >
      <AlertTriangle size={16} className="shrink-0 mt-0.5" />
      <div className="min-w-0 flex flex-col gap-1">
        {alerts.map(alert => {
          const users = [
            ...(alert.souls ?? []),
            ...(alert.reviewer ? [t("chat.model_lifecycle.reviewer")] : []),
            ...(alert.summarizer ? [t("chat.model_lifecycle.summarizer")] : []),
          ].join(t("chat.model_lifecycle.separator"));
          const date = alert.retires_at ? formatToTaipeiDate(alert.retires_at * MS_PER_SECOND) : "";
          return (
            <p key={alert.model}>
              {t(MESSAGE_KEYS[alert.status], { model: alert.model, date })}{" "}
              {t("chat.model_lifecycle.used_by", { users })}
            </p>
          );
        })}
      </div>
    </div>
  );
}
