"use client";

import { AlertTriangle, LayoutDashboard } from "lucide-react";
import { useTranslation } from "react-i18next";

interface SidebarIdentityProps {
  mode: string;
  checkState?: string;
  /** 記憶提示有警告級時為 true；顯示警告標記（詳情在右側欄）。 */
  memoryWarning?: boolean;
  /** 系統提示有警告級時為 true（設定讀不到而停掉功能）；比記憶警告優先標示。 */
  systemWarning?: boolean;
}

export function SidebarIdentity({
  mode, checkState = "checking", memoryWarning = false, systemWarning = false,
}: SidebarIdentityProps) {
  const { t } = useTranslation();
  const label = mode === "hybrid" || mode === "standalone" ? mode : checkState;
  const warningLabel = t(`sidebar.${systemWarning ? "system_notice" : "memory_notice"}.warning_marker`);
  return (
    <div className="flex gap-3 items-center w-full px-2">
      <div className="bg-primary rounded-xl p-1.5 text-white w-12 h-12 flex items-center justify-center shrink-0 shadow-sm">
        <LayoutDashboard className="w-6 h-6" />
      </div>
      <div className="flex flex-col">
        <h1 className="text-foreground text-base font-semibold leading-tight">Touka</h1>
        {/* WCAG 1.4.3（A5 axe）：muted-foreground 疊 opacity-70 實測不達 4.5 → 去 opacity */}
        <p className="text-xs font-bold uppercase tracking-widest text-muted-foreground">
          {t(`sidebar.system_status.${label}`)}
        </p>
      </div>
      {(memoryWarning || systemWarning) && (
        <span role="img" aria-label={warningLabel} title={warningLabel} className="ml-auto shrink-0 text-destructive">
          <AlertTriangle className="w-4 h-4" />
        </span>
      )}
    </div>
  );
}
