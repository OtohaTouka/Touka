import { AlertTriangle } from "lucide-react";
import { useTranslation } from "react-i18next";

interface DegradedBannerProps {
  degraded: boolean;
  reason?: string;
}

/**
 * 系統降級常駐橫幅（P5b polish）：記憶系統（ToukaMemory）未載入時，Agent 無法使用
 * 長期記憶與部分工具。此狀態由後端啟動時偵測（/v1/memory 的 system_degraded，
 * 經 SystemStatusContext 取得），降級期間於側欄頂端常駐顯示、不隨分頁切換消失。
 * 系統正常時整個元件不顯示。
 */
export function DegradedBanner({ degraded, reason }: DegradedBannerProps) {
  const { t } = useTranslation();
  if (!degraded) return null;
  const detail = reason?.trim() || t("sidebar.degraded_banner.default_detail");
  return (
    <div
      role="alert"
      data-testid="degraded-banner"
      className="w-full bg-destructive text-destructive-foreground text-xs px-4 py-2 flex items-start gap-2 shrink-0"
    >
      <AlertTriangle size={14} className="shrink-0 mt-0.5" />
      <span className="min-w-0">
        {t("sidebar.degraded_banner.message", { detail })}
      </span>
    </div>
  );
}
