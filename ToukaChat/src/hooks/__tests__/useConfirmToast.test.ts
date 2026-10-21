import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';

vi.mock('sonner', () => ({ toast: { warning: vi.fn() } }));

import { toast } from 'sonner';
import { useConfirmToast } from '../useConfirmToast';

const mockWarning = toast.warning as ReturnType<typeof vi.fn>;

describe('useConfirmToast', () => {
    beforeEach(() => vi.clearAllMocks());

    it('點確認時呼叫 onConfirm', () => {
        const { result } = renderHook(() => useConfirmToast());
        const onConfirm = vi.fn();
        result.current.confirmToast('msg', onConfirm);
        const opts = mockWarning.mock.calls[0][1];
        opts.action.onClick();
        expect(onConfirm).toHaveBeenCalled();
    });

    it('點取消時呼叫 onCancel（且不呼叫 onConfirm）', () => {
        const { result } = renderHook(() => useConfirmToast());
        const onConfirm = vi.fn();
        const onCancel = vi.fn();
        result.current.confirmToast('msg', onConfirm, undefined, onCancel);
        const opts = mockWarning.mock.calls[0][1];
        opts.cancel.onClick();
        expect(onCancel).toHaveBeenCalled();
        expect(onConfirm).not.toHaveBeenCalled();
    });

    it('未提供 onCancel 時點取消不會拋錯', () => {
        const { result } = renderHook(() => useConfirmToast());
        result.current.confirmToast('msg', vi.fn());
        const opts = mockWarning.mock.calls[0][1];
        expect(() => opts.cancel.onClick()).not.toThrow();
    });
});
