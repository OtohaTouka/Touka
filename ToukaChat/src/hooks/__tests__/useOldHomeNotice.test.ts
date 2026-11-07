/**
 * ADR-0306：舊家不能開新的一輪（SESSION_OLD_HOME_READ_ONLY）→ toast 顯示專屬提示；
 * 其他錯誤不顯示、交回呼叫端。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { toast } from 'sonner';
import { errorMsg, ToukaApiError } from '@/lib/error_handler';
import { useOldHomeNotice } from '../useOldHomeNotice';

vi.mock('sonner', () => ({ toast: { warning: vi.fn(), error: vi.fn() } }));

describe('useOldHomeNotice（ADR-0306）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('SESSION_OLD_HOME_READ_ONLY → 顯示舊家的提示', () => {
    const { result } = renderHook(() => useOldHomeNotice());
    expect(result.current(new ToukaApiError('x', 409, 'SESSION_OLD_HOME_READ_ONLY'))).toBe(true);
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(errorMsg('SESSION_OLD_HOME_READ_ONLY'));
  });

  it.each([
    ['其他錯誤碼', new ToukaApiError('x', 403, 'HOOK_PROMPT_BLOCKED', 'r')],
    ['沒有碼的 API 錯誤', new ToukaApiError('x', 409)],
    ['一般 Error', new Error('SESSION_OLD_HOME_READ_ONLY')],
  ])('%s → 不處理、不顯示', (_label, err) => {
    const { result } = renderHook(() => useOldHomeNotice());
    expect(result.current(err)).toBe(false);
    expect(toast.error).not.toHaveBeenCalled();
  });
});
