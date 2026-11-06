import { useCallback } from 'react';
import { toast } from 'sonner';
import { errorMsg, getErrorCode, ToukaApiError } from '@/lib/error_handler';

/** 理由最多顯示的字數（後端已截斷，這裡再防一次過長的提示）。 */
const MAX_REASON_CHARS = 2000;

/**
 * 管理員設定的 UserPromptSubmit hook 擋下這則訊息（HOOK_PROMPT_BLOCKED，ADR-0292）時，
 * 顯示擋下的提示與 hook 給的理由。
 *
 * 回傳 notify(err)：處理了這個錯誤回 true；其他錯誤回 false，由呼叫端照原本方式顯示。
 */
export function useHookBlockNotice(): (error: unknown) => boolean {
  return useCallback((error: unknown) => {
    if (getErrorCode(error) !== 'HOOK_PROMPT_BLOCKED') return false;
    const reason = error instanceof ToukaApiError ? error.reason?.slice(0, MAX_REASON_CHARS) : undefined;
    toast.error(errorMsg('HOOK_PROMPT_BLOCKED'), reason ? { description: reason } : undefined);
    return true;
  }, []);
}
