import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

// 攔截確認提示，讓測試可手動觸發「確認 / 取消」。
let captured: { onConfirm: () => void; onCancel?: () => void } | null = null;
vi.mock('@/hooks/useConfirmToast', () => ({
    useConfirmToast: () => ({
        confirmToast: (_m: string, onConfirm: () => void, _l?: string, onCancel?: () => void) => {
            captured = { onConfirm, onCancel };
        },
    }),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));
vi.mock('@/lib/connections', () => ({ captureRequestScope: () => ({}), isRequestScopeCurrent: () => true }));

import { toast } from 'sonner';
import { useSessionEvents } from '../useSessionEvents';

const mkSession = (id: string, agent_id?: string): any => ({
    id, title: id, messages: { messages: {}, currentId: null }, agent_id, updatedAt: 1,
});

function dispatchImport(detail: unknown) {
    window.dispatchEvent(new CustomEvent('touka_import_chats', { detail }));
}

describe('useSessionEvents 匯入確認', () => {
    beforeEach(() => { captured = null; vi.clearAllMocks(); });

    it('重播合併 updater 不重送，只有所有保存確認才提示成功', async () => {
        const setSessions = vi.fn(action => { if (typeof action === 'function') { action([]); action([]); } });
        const syncSession = vi.fn().mockResolvedValue({ status: 'unknown', error_code: 1302 });
        renderHook(() => useSessionEvents([], setSessions, syncSession));
        await act(async () => { dispatchImport([{ id: 'x', agent_id: 'Alice' }]); });
        await act(async () => { captured!.onConfirm(); });
        expect(syncSession).toHaveBeenCalledTimes(1);
        expect(toast.success).not.toHaveBeenCalled();
        expect(toast.warning).toHaveBeenCalled();
    });

    it('確認前不合併；確認後才合併並提示成功', async () => {
        const setSessions = vi.fn();
        const syncSession = vi.fn().mockResolvedValue({ status: 'saved', representation: 'full', revision: 'r1' });
        renderHook(() => useSessionEvents([mkSession('a', 'Alice')], setSessions, syncSession));

        await act(async () => {
            dispatchImport([{ id: 'a', title: 'A2', agent_id: 'Alice' }]); // 撞 id → 覆蓋警告
            await Promise.resolve();
        });
        expect(captured).not.toBeNull();
        expect(setSessions).not.toHaveBeenCalled();

        await act(async () => { captured!.onConfirm(); });
        await waitFor(() => expect(setSessions).toHaveBeenCalled());
        expect(toast.success).toHaveBeenCalled();
    });

    it('取消則不合併、不提示成功', async () => {
        const setSessions = vi.fn();
        renderHook(() => useSessionEvents([], setSessions, vi.fn()));

        await act(async () => {
            dispatchImport([{ id: 'x', title: 'X', agent_id: 'Alice' }]);
            await Promise.resolve();
        });
        expect(captured).not.toBeNull();

        await act(async () => { captured!.onCancel?.(); await Promise.resolve(); });
        expect(setSessions).not.toHaveBeenCalled();
        expect(toast.success).not.toHaveBeenCalled();
    });
});
