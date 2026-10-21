import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import { BackgroundActivityMap } from "@/hooks/useSystemMonitoring";

interface BackgroundActivityProps {
  activity: BackgroundActivityMap;
}

/**
 * 背景活動：顯示目前在背景跑的 session（如心跳多脈衝進度 current/total）。
 * 背景脈衝是 detached、無 SSE，否則完全不可見；無活動時整個元件不顯示。
 */
export function BackgroundActivity({ activity }: BackgroundActivityProps) {
  const { t } = useTranslation();
  const items = Object.entries(activity || {});
  if (items.length === 0) return null;
  return (
    // WCAG 4.1.3：背景脈衝進度變化須讓螢幕閱讀器聽見
    <div
      role="status"
      className="bg-primary/5 p-3 rounded-xl border border-primary/20 space-y-2"
      data-testid="background-activity"
    >
      <div className="flex items-center gap-2 text-xs font-bold text-primary uppercase">
        <Loader2 size={12} className="animate-spin" />
        {t("sidebar.background_activity.running_in_background")}
      </div>
      {items.map(([sid, a]) => (
        <div key={sid} className="flex items-center justify-between text-xs gap-2 pl-1">
          <span className="font-bold text-muted-foreground uppercase truncate">{a.agent || sid}</span>
          <span className="font-mono text-primary font-bold whitespace-nowrap">
            {t("sidebar.background_activity.pulse_label")} {a.current ?? "?"}/{a.total ?? "?"}
          </span>
        </div>
      ))}
    </div>
  );
}
