import { useTranslation } from "react-i18next";
import { AlertTriangle } from "lucide-react";
import type { FailedHandover } from "@/hooks/useSystemMonitoring";

/** 換家交接失敗三次（ADR-0307）：舊家照舊唯讀，系統之後會自動再試。 */
export function FailedHandoversNotice({ handovers }: { handovers: FailedHandover[] }) {
  const { t } = useTranslation();
  if (!handovers?.length) return null;
  return (
    <div
      role="status"
      className="bg-status-warning/10 border-status-warning/30 space-y-2 rounded-xl border p-3"
      data-testid="failed-handovers-notice"
    >
      <div className="text-status-warning flex items-center gap-2 text-xs font-bold uppercase">
        <AlertTriangle size={12} />
        {t("sidebar.failed_handovers_notice.heading", { count: handovers.length })}
      </div>
      <div className="space-y-1">
        {handovers.map((item) => (
          <div key={`${item.agent ?? ""}:${item.old_session ?? ""}`} className="text-muted-foreground truncate pl-1 text-xs">
            {item.agent || item.old_session || t("sidebar.failed_handovers_notice.unknown")}
          </div>
        ))}
      </div>
      <p className="text-muted-foreground pl-1 text-xs">{t("sidebar.failed_handovers_notice.hint")}</p>
    </div>
  );
}
