/**
 * model-p0 U7：代理人沒設主要模型（AGENT_BRAIN_NOT_SET）→ toast 請使用者選，
 * 按鈕送出既有的 touka_switch_view 事件切到靈魂設定頁；其他錯誤不顯示、交回呼叫端。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { toast } from 'sonner';
import { ToukaApiError } from '@/lib/error_handler';
import { useAgentBrainNotice } from '../useAgentBrainNotice';

vi.mock('sonner', () => ({ toast: { warning: vi.fn(), error: vi.fn() } }));

type ToastOptions = { action: { label: string; onClick: () => void } };

describe('useAgentBrainNotice（model-p0 U7）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('AGENT_BRAIN_NOT_SET → 顯示通知，按鈕切到靈魂設定頁', () => {
    const { result } = renderHook(() => useAgentBrainNotice());
    expect(result.current(new ToukaApiError('x', 409, 'AGENT_BRAIN_NOT_SET'))).toBe(true);

    expect(toast.warning).toHaveBeenCalledTimes(1);
    const [message, options] = vi.mocked(toast.warning).mock.calls[0] as unknown as [string, ToastOptions];
    expect(message).toBe('這個代理人還沒設定主要模型，請選一個。');
    expect(options.action.label).toBe('去設定');

    const listener = vi.fn();
    window.addEventListener('touka_switch_view', listener);
    options.action.onClick();
    window.removeEventListener('touka_switch_view', listener);
    expect(listener).toHaveBeenCalledTimes(1);
    expect((listener.mock.calls[0][0] as CustomEvent<string>).detail).toBe('soul');
  });

  it.each([
    ['其他錯誤碼', new ToukaApiError('x', 404, 'MODEL_NOT_FOUND')],
    ['沒有碼的 API 錯誤', new ToukaApiError('x', 500)],
    ['一般 Error', new Error('AGENT_BRAIN_NOT_SET')],
    ['使用者中止', { name: 'AbortError' }],
  ])('%s → 不顯示、回 false', (_name, err) => {
    const { result } = renderHook(() => useAgentBrainNotice());
    expect(result.current(err)).toBe(false);
    expect(toast.warning).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });
});
