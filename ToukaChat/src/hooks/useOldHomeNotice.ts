import { useCallback } from 'react';
import { toast } from 'sonner';
import { errorMsg, getErrorCode } from '@/lib/error_handler';

/**
 * 舊家（換家之後、交接完成前的那段家對話，ADR-0306）不能開新的一輪：後端在送模型之前就擋下
 * （SESSION_OLD_HOME_READ_ONLY）。顯示專屬提示，不顯示成連線錯誤。
 *
 * 回傳 notify(err)：處理了這個錯誤回 true；其他錯誤回 false，由呼叫端照原本方式顯示。
 */
export function useOldHomeNotice(): (error: unknown) => boolean {
  return useCallback((error: unknown) => {
    if (getErrorCode(error) !== 'SESSION_OLD_HOME_READ_ONLY') return false;
    toast.error(errorMsg('SESSION_OLD_HOME_READ_ONLY'));
    return true;
  }, []);
}
