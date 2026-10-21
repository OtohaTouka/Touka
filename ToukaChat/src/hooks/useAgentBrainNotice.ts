import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { getErrorCode } from '@/lib/error_handler';

/** 既有的全域切換畫面事件（app/page.tsx 監聽）。 */
const SWITCH_VIEW_EVENT = 'touka_switch_view';

/**
 * 代理人還沒設定主要模型（AGENT_BRAIN_NOT_SET）時，提示使用者到靈魂設定頁選一個。
 *
 * 回傳 notify(err)：處理了這個錯誤回 true；其他錯誤回 false，由呼叫端照原本方式顯示。
 * 不存任何狀態；新增需要專屬提示的錯誤碼時另寫 hook，不改聊天主流程（OCP）。
 */
export function useAgentBrainNotice(): (error: unknown) => boolean {
  const { t } = useTranslation();
  return useCallback((error: unknown) => {
    if (getErrorCode(error) !== 'AGENT_BRAIN_NOT_SET') return false;
    toast.warning(t('hooks.use_agent_brain_notice.message'), {
      action: {
        label: t('hooks.use_agent_brain_notice.action'),
        onClick: () => window.dispatchEvent(new CustomEvent(SWITCH_VIEW_EVENT, { detail: 'soul' })),
      },
    });
    return true;
  }, [t]);
}
