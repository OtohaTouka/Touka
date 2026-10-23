"use client";

import { KeyRound, Wrench } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useSetupNotices } from "@/components/setup/SetupNoticesProvider";
import type { WizardStepId } from "@/components/setup/setupWizardConfig";

interface SidebarSetupNoticesProps {
  /** 按下打開精靈的按鈕後呼叫：手機版用來關掉選單，否則選單會擋住精靈。 */
  onAction?: () => void;
}

const CARD = "rounded-xl text-xs px-3 py-2 flex items-start gap-2";
const LINK = "underline font-semibold whitespace-nowrap hover:opacity-80";

/**
 * 左側邊欄的首次設定警告（只給管理員；條件與狀態見 SetupNoticesProvider）。
 * ①管理員密碼還是臨時的：紅色、不能關；②初次設定還沒完成：可按「稍後」。
 * 沒有警告時回 null，不在側欄留空白。
 */
export function SidebarSetupNotices({ onAction }: SidebarSetupNoticesProps) {
  const { t } = useTranslation();
  const { showTempPassword, showSetupPrompt, openWizard, dismissPrompt } = useSetupNotices();
  if (!showTempPassword && !showSetupPrompt) return null;

  const open = (stepId?: WizardStepId) => {
    openWizard(stepId);
    onAction?.();
  };

  return (
    <div className="w-full px-2 flex flex-col gap-1.5">
      {showTempPassword && (
        <div role="alert" className={`${CARD} bg-destructive text-destructive-foreground`}>
          <KeyRound size={14} className="shrink-0 mt-0.5" />
          <p className="min-w-0 break-words">
            {t("sidebar.setup_notice.temp_password")}{" "}
            <button type="button" onClick={() => open("admin_password")} className={LINK}>
              {t("sidebar.setup_notice.change_password")}
            </button>
          </p>
        </div>
      )}
      {showSetupPrompt && (
        <div role="status" className={`${CARD} bg-accent text-accent-foreground`}>
          <Wrench size={14} className="shrink-0 mt-0.5" />
          <div className="min-w-0 flex flex-col gap-1">
            <p className="break-words">{t("sidebar.setup_notice.setup_incomplete")}</p>
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              <button type="button" onClick={() => open()} className={LINK}>
                {t("sidebar.setup_notice.setup_now")}
              </button>
              <button
                type="button"
                onClick={dismissPrompt}
                aria-label={t("sidebar.setup_notice.dismiss_aria")}
                className="font-medium whitespace-nowrap hover:opacity-80"
              >
                {t("sidebar.setup_notice.later")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
