/**
 * ADR-0292：這一則在開始串流前就被別處停止（PROMPT_STOPPED）→ 顯示一次停止的提示，不是錯誤；
 * 其他錯誤不顯示、交回呼叫端。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { toast } from 'sonner';
import { errorMsg, ToukaApiError } from '@/lib/error_handler';
import { usePromptStoppedNotice } from '../usePromptStoppedNotice';

vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn() } }));

describe('usePromptStoppedNotice（ADR-0292）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('PROMPT_STOPPED → 顯示一次停止的提示，不當成錯誤', () => {
    const { result } = renderHook(() => usePromptStoppedNotice());
    expect(result.current(new ToukaApiError('x', 409, 'PROMPT_STOPPED'))).toBe(true);

    expect(toast.info).toHaveBeenCalledTimes(1);
    expect(toast.info).toHaveBeenCalledWith(errorMsg('PROMPT_STOPPED'));
    expect(toast.error).not.toHaveBeenCalled();
  });

  it.each([
    ['其他錯誤碼', new ToukaApiError('x', 403, 'HOOK_PROMPT_BLOCKED', 'r')],
    ['沒有碼的 API 錯誤', new ToukaApiError('x', 409)],
    ['一般 Error', new Error('PROMPT_STOPPED')],
    ['使用者中止', { name: 'AbortError' }],
  ])('%s → 不顯示、回 false', (_name, err) => {
    const { result } = renderHook(() => usePromptStoppedNotice());
    expect(result.current(err)).toBe(false);
    expect(toast.info).not.toHaveBeenCalled();
  });
});
