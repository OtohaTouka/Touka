"use client";

import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { PanelLeftOpen, PanelRightClose, PanelRightOpen, Settings } from "lucide-react";
import { cn } from "@/lib/utils";
import { SetupNoticeDot } from "@/components/setup/SetupNoticeDot";
import { useHasSetupNotice } from "@/components/setup/SetupNoticesProvider";

interface MainAreaControlsProps {
  isIDE: boolean;
  isLeftSidebarCollapsed: boolean;
  onExpandLeftSidebar: () => void;
  isRightSidebarOpen: boolean;
  onToggleRightSidebar: () => void;
  /** IDE 沒有左側邊欄，設定鈕是唯一入口；設定頁開著時再按一次回對話。 */
  isSettingsOpen: boolean;
  onToggleSettings: () => void;
}

const ICON_BUTTON =
  "relative p-1.5 rounded-lg transition-colors shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40";

/**
 * 對話區最上面的細列（取代上導覽列，桌面版與 IDE 用；手機版有自己的頂列）。
 * 左上角：左側邊欄收起時的展開鈕（帶首次設定警告的紅點）。右上角：工具面板鈕；IDE 改放設定鈕。
 */
export function MainAreaControls({
  isIDE,
  isLeftSidebarCollapsed,
  onExpandLeftSidebar,
  isRightSidebarOpen,
  onToggleRightSidebar,
  isSettingsOpen,
  onToggleSettings,
}: MainAreaControlsProps) {
  const { t } = useTranslation();
  const hasSetupNotice = useHasSetupNotice();
  const expandRef = useRef<HTMLButtonElement>(null);
  const wasCollapsed = useRef(isLeftSidebarCollapsed);

  // WCAG 2.4.3：側欄剛收起時，焦點原本在側欄裡（收合鈕已看不見）或掉到 body，就交給展開鈕。
  useEffect(() => {
    const justCollapsed = isLeftSidebarCollapsed && !wasCollapsed.current;
    wasCollapsed.current = isLeftSidebarCollapsed;
    if (!justCollapsed) return;
    const active = document.activeElement;
    if (!active || active === document.body || active.closest("#left-sidebar-panel")) {
      expandRef.current?.focus();
    }
  }, [isLeftSidebarCollapsed]);

  const expandLabel = t("layout.main_area_controls.expand_sidebar");
  const rightLabel = t("layout.main_area_controls.right_sidebar");
  const settingsLabel = t("layout.main_area_controls.settings");

  return (
    <div className="h-10 shrink-0 flex items-center justify-between px-2 bg-background">
      <div className="flex items-center">
        {!isIDE && isLeftSidebarCollapsed && (
          <button
            ref={expandRef}
            type="button"
            onClick={onExpandLeftSidebar}
            aria-label={hasSetupNotice ? t("sidebar.setup_notice.expand_sidebar_aria") : expandLabel}
            title={expandLabel}
            className={cn(ICON_BUTTON, "text-muted-foreground hover:text-foreground hover:bg-sidebar")}
          >
            <PanelLeftOpen size={18} />
            <SetupNoticeDot />
          </button>
        )}
      </div>
      <div className="flex items-center">
        {isIDE ? (
          <button
            type="button"
            onClick={onToggleSettings}
            aria-label={settingsLabel}
            aria-pressed={isSettingsOpen}
            title={settingsLabel}
            className={cn(
              ICON_BUTTON,
              isSettingsOpen ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-sidebar"
            )}
          >
            <Settings size={16} />
          </button>
        ) : (
          <button
            type="button"
            onClick={onToggleRightSidebar}
            aria-label={rightLabel}
            aria-expanded={isRightSidebarOpen}
            aria-controls="right-sidebar-panel"
            title={rightLabel}
            className={cn(
              ICON_BUTTON,
              isRightSidebarOpen ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground hover:bg-sidebar"
            )}
          >
            {isRightSidebarOpen ? <PanelRightClose size={18} /> : <PanelRightOpen size={18} />}
          </button>
        )}
      </div>
    </div>
  );
}
