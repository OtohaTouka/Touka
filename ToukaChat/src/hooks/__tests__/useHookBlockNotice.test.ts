/**
 * ADR-0292：UserPromptSubmit hook 擋下訊息（HOOK_PROMPT_BLOCKED）→ toast 顯示擋下與 hook 的理由；
 * 其他錯誤不顯示、交回呼叫端。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { toast } from 'sonner';
import { errorMsg, ToukaApiError } from '@/lib/error_handler';
import { useHookBlockNotice } from '../useHookBlockNotice';

vi.mock('sonner', () => ({ toast: { warning: vi.fn(), error: vi.fn() } }));

describe('useHookBlockNotice（ADR-0292）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('HOOK_PROMPT_BLOCKED → 顯示擋下的提示，理由放在說明', () => {
    const { result } = renderHook(() => useHookBlockNotice());
    const err = new ToukaApiError('x', 403, 'HOOK_PROMPT_BLOCKED', '這則訊息含有不允許的內容');
    expect(result.current(err)).toBe(true);

    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(errorMsg('HOOK_PROMPT_BLOCKED'), {
      description: '這則訊息含有不允許的內容',
    });
  });

  it('沒有理由 → 只顯示擋下的提示；過長的理由截斷', () => {
    const { result } = renderHook(() => useHookBlockNotice());
    result.current(new ToukaApiError('x', 403, 'HOOK_PROMPT_BLOCKED'));
    expect(toast.error).toHaveBeenLastCalledWith(errorMsg('HOOK_PROMPT_BLOCKED'), undefined);

    result.current(new ToukaApiError('x', 403, 'HOOK_PROMPT_BLOCKED', 'a'.repeat(5000)));
    const options = vi.mocked(toast.error).mock.calls[1][1] as { description: string };
    expect(options.description).toHaveLength(2000);
  });

  it.each([
    ['其他錯誤碼', new ToukaApiError('x', 409, 'AGENT_BRAIN_NOT_SET', 'r')],
    ['沒有碼的 API 錯誤', new ToukaApiError('x', 403)],
    ['一般 Error', new Error('HOOK_PROMPT_BLOCKED')],
    ['使用者中止', { name: 'AbortError' }],
  ])('%s → 不顯示、回 false', (_name, err) => {
    const { result } = renderHook(() => useHookBlockNotice());
    expect(result.current(err)).toBe(false);
    expect(toast.error).not.toHaveBeenCalled();
  });
});
