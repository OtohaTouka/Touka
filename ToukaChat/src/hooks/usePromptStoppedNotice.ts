import { useCallback } from 'react';
import { toast } from 'sonner';
import { errorMsg, getErrorCode } from '@/lib/error_handler';

/**
 * 這一則在開始串流前就被停止（PROMPT_STOPPED，ADR-0292）時提示一次。
 *
 * 自己按停止時前端已先當成中止，不會走到這裡；會收到的是別處停止了同一個對話（另一個分頁或
 * 裝置、重置靈魂）。呼叫端照中止處理，不顯示連線錯誤。
 *
 * 回傳 notify(err)：處理了這個錯誤回 true；其他錯誤回 false，由呼叫端照原本方式顯示。
 */
export function usePromptStoppedNotice(): (error: unknown) => boolean {
  return useCallback((error: unknown) => {
    if (getErrorCode(error) !== 'PROMPT_STOPPED') return false;
    toast.info(errorMsg('PROMPT_STOPPED'));
    return true;
  }, []);
}
