import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useConfirmToast } from '@/hooks/useConfirmToast';
import { toast } from 'sonner';

vi.mock('sonner', () => ({
  toast: {
    warning: vi.fn(),
  },
}));

describe('useConfirmToast', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns a confirmToast function', () => {
    const { result } = renderHook(() => useConfirmToast());
    expect(typeof result.current.confirmToast).toBe('function');
  });

  it('calls toast.warning with message, action and cancel options', () => {
    const { result } = renderHook(() => useConfirmToast());

    result.current.confirmToast('確定要刪除嗎？', vi.fn());

    expect(toast.warning).toHaveBeenCalledWith(
      '確定要刪除嗎？',
      expect.objectContaining({
        action: expect.objectContaining({ label: '確認' }),
        cancel: expect.objectContaining({ label: '取消', onClick: expect.any(Function) }),
        duration: Infinity,
      }),
    );
  });

  it('invokes onConfirm when the action button is clicked', () => {
    const { result } = renderHook(() => useConfirmToast());
    const onConfirm = vi.fn();

    result.current.confirmToast('確定要刪除嗎？', onConfirm);

    const [, options] = (toast.warning as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      { action: { onClick: () => void } },
    ];
    options.action.onClick();

    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('supports a custom confirm label', () => {
    const { result } = renderHook(() => useConfirmToast());

    result.current.confirmToast('確定要覆蓋嗎？', vi.fn(), '確認覆蓋');

    expect(toast.warning).toHaveBeenCalledWith(
      '確定要覆蓋嗎？',
      expect.objectContaining({
        action: expect.objectContaining({ label: '確認覆蓋' }),
      }),
    );
  });
});
