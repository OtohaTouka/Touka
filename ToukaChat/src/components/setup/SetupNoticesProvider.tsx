'use client';

import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus, type SetupStatus } from '@/hooks/useSetupStatus';
import { SetupWizard } from './SetupWizard';
import type { WizardStepId } from './setupWizardConfig';

interface SetupNotices {
  /** 管理員密碼還是臨時的：紅色警告，不能關。 */
  showTempPassword: boolean;
  /** 初次設定還沒完成，而且這次開頁還沒按「稍後」。 */
  showSetupPrompt: boolean;
  openWizard: (stepId?: WizardStepId) => void;
  dismissPrompt: () => void;
}

// 沒有提供者（例如單獨測試的元件）時什麼都不顯示。
const NO_NOTICES: SetupNotices = {
  showTempPassword: false,
  showSetupPrompt: false,
  openWizard: () => {},
  dismissPrompt: () => {},
};

const SetupNoticesContext = createContext<SetupNotices>(NO_NOTICES);

function hasSetupIssues(status: SetupStatus): boolean {
  return (
    status.dispatch_token_is_default ||
    !status.auth_secret_persisted ||
    status.api_keys_configured.length === 0 ||
    status.is_admin_temp_password
  );
}

/**
 * 首次設定警告的狀態與設定精靈（ADR-0053）。警告本身畫在左側邊欄（SidebarSetupNotices），
 * 這裡只保管狀態、不畫任何版面元素，避免又在最外層排出一條直欄。
 * 必須放在 AuthGuard 裡：登出、登入過期或換使用者時跟著卸載，「稍後」才會重置。
 */
export function SetupNoticesProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const { status, refetch } = useSetupStatus();
  // 重讀時 hook 會先把狀態設成 null；保留上次讀到的狀態，重讀失敗也不讓警告消失。
  // 這裡依賴 hook 回傳的 status 在兩次讀取之間是同一個物件（它直接回傳 state）。
  const [lastStatus, setLastStatus] = useState<SetupStatus | null>(null);
  if (status !== null && status !== lastStatus) setLastStatus(status);
  const [promptDismissed, setPromptDismissed] = useState(false);
  const [wizard, setWizard] = useState<{ stepId?: WizardStepId } | null>(null);

  const shown = status ?? lastStatus;
  const isAdmin = user?.role === 'admin';
  const value = useMemo<SetupNotices>(() => ({
    showTempPassword: isAdmin && shown !== null && shown.is_admin_temp_password,
    showSetupPrompt: isAdmin && !promptDismissed && shown !== null && hasSetupIssues(shown),
    openWizard: (stepId) => setWizard({ stepId }),
    dismissPrompt: () => setPromptDismissed(true),
  }), [isAdmin, shown, promptDismissed]);

  return (
    <SetupNoticesContext.Provider value={value}>
      {children}
      {wizard && (
        <SetupWizard
          status={shown}
          initialStepId={wizard.stepId}
          onClose={() => {
            setWizard(null);
            refetch();
          }}
        />
      )}
    </SetupNoticesContext.Provider>
  );
}

export function useSetupNotices(): SetupNotices {
  return useContext(SetupNoticesContext);
}

/** 左側邊欄裡有沒有正在顯示的首次設定警告（側欄開關按鈕的紅點與名稱用）。 */
export function useHasSetupNotice(): boolean {
  const { showTempPassword, showSetupPrompt } = useSetupNotices();
  return showTempPassword || showSetupPrompt;
}
