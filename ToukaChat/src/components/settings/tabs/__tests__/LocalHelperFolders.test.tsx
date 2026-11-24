import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import React from 'react';
import '@/lib/i18n';

type Folders =
    | { kind: 'loading' }
    | { kind: 'denied' }
    | { kind: 'failed' }
    | { kind: 'loaded'; list: Record<string, unknown> };

type RevokeOutcome =
    | { kind: 'result'; result: Record<string, unknown> }
    | { kind: 'denied'; code: string | null }
    | { kind: 'failed' };

const state: { folders: Folders; enabledWith: boolean[]; refreshWith: number[] } = {
    folders: { kind: 'loading' },
    enabledWith: [],
    refreshWith: [],
};
vi.mock('@/hooks/settings/useLocalHelperFolders', () => ({
    useLocalHelperFolders: (enabled: boolean, refresh: number) => {
        state.enabledWith.push(enabled);
        state.refreshWith.push(refresh);
        return state.folders;
    },
}));

const revoking = {
    pending: false,
    outcome: null as RevokeOutcome | null,
    revoke: vi.fn(async (_folder: { name: string; path: string }, _change: string): Promise<RevokeOutcome | null> => null),
};
vi.mock('@/hooks/settings/useLocalFolderRevoke', () => ({ useLocalFolderRevoke: () => revoking }));

import { LocalHelperFolders } from '../LocalHelperFolders';

const ONLINE = { paired: true, online: true, helper_version: '0.6.0', version_problem: null };

function listed(changes: Record<string, unknown> = {}): Folders {
    return {
        kind: 'loaded',
        list: {
            state: 'ok',
            exposed: false,
            folders: [
                { name: 'Notes', path: 'D:\\Example\\Notes', writable: false, usable: true },
                { name: 'Drafts', path: 'E:\\Drafts', writable: true, usable: false },
            ],
            omitted: 0,
            ...changes,
        },
    };
}

function done(changes: Record<string, unknown>): RevokeOutcome {
    return { kind: 'result', result: { status: 'done', code: null, name: 'Notes', removed: null, writable: null, ...changes } };
}

function failed(code: string): RevokeOutcome {
    return { kind: 'result', result: { status: 'error', code, name: null, removed: null, writable: null } };
}

describe('LocalHelperFolders（ADR-0330）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        state.folders = { kind: 'loading' };
        state.enabledWith = [];
        state.refreshWith = [];
        revoking.pending = false;
        revoking.outcome = null;
    });

    it('小幫手沒開著時不顯示、也不讀', () => {
        const { container } = render(
            <LocalHelperFolders status={{ ...ONLINE, online: false }} />,
        );
        expect(container.textContent).toBe('');
        expect(state.enabledWith.every((enabled) => !enabled)).toBe(true);
    });

    it('列出名稱、完整路徑、權限與暫停中', () => {
        state.folders = listed();
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByText('開放中的資料夾')).toBeDefined();
        expect(screen.getByText('Notes')).toBeDefined();
        expect(screen.getByText('D:\\Example\\Notes')).toBeDefined();
        expect(screen.getByText('E:\\Drafts')).toBeDefined();
        expect(screen.getByText('只能看')).toBeDefined();
        expect(screen.getByText('可以改')).toBeDefined();
        expect(screen.getByText('暫停中')).toBeDefined();
        // 以主視窗為準，並說怎麼叫出主視窗
        expect(screen.getByText(/ToukaLocal\.cmd/)).toBeDefined();
    });

    it('小幫手被看見時警告，也不能在這裡改', () => {
        state.folders = listed({ exposed: true });
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByText(/為了安全，小幫手停止服務中/)).toBeDefined();
        expect(screen.queryByRole('button', { name: /拿掉/ })).toBeNull();
        expect(screen.queryByRole('button', { name: /改成只能讀/ })).toBeNull();
    });

    it('沒列出的個數', () => {
        state.folders = listed({ omitted: 3 });
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByText(/還有 3 個沒有列出/)).toBeDefined();
    });

    it.each([
        ['waiting', '正在等小幫手回報開放中的資料夾。'],
        ['stale', '小幫手一陣子沒有回報，看不到目前開放的資料夾。'],
        ['offline', '小幫手沒有連線，看不到目前開放的資料夾。'],
    ])('%s：不顯示舊清單', (listState, text) => {
        state.folders = { kind: 'loaded', list: { state: listState, exposed: false, folders: null, omitted: 0 } };
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByText(text)).toBeDefined();
        expect(screen.queryByText('Notes')).toBeNull();
    });

    it('一個都沒開放', () => {
        state.folders = listed({ folders: [] });
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByText('還沒有開放任何資料夾。')).toBeDefined();
    });

    it('不是配對的人', () => {
        state.folders = { kind: 'denied' };
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByText('只有產生配對碼的人看得到開放中的資料夾。')).toBeDefined();
    });

    it('讀不到', () => {
        state.folders = { kind: 'failed' };
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByText('讀不到開放中的資料夾，請稍後再試。')).toBeDefined();
    });

    // --- 拿掉、改成只能讀（ADR-0330 2b） ---

    it('可以改的有兩個按鈕，只能看的只有「拿掉」', () => {
        state.folders = listed();
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByRole('button', { name: '把「Drafts」改成只能讀' })).toBeDefined();
        expect(screen.getByRole('button', { name: '拿掉「Drafts」' })).toBeDefined();
        expect(screen.getByRole('button', { name: '拿掉「Notes」' })).toBeDefined();
        expect(screen.queryByRole('button', { name: '把「Notes」改成只能讀' })).toBeNull();
    });

    it('按了先確認，取消就不送', () => {
        state.folders = listed();
        render(<LocalHelperFolders status={ONLINE} />);
        fireEvent.click(screen.getByRole('button', { name: '拿掉「Notes」' }));
        expect(screen.getByText('確定從開放名單拿掉「Notes」？代理人就用不到它了；要加回請到小幫手主視窗。')).toBeDefined();
        fireEvent.click(screen.getByRole('button', { name: '取消' }));
        expect(revoking.revoke).not.toHaveBeenCalled();
        expect(screen.queryByText(/確定從開放名單拿掉/)).toBeNull();
    });

    it('確認後送出名稱、路徑與改法，成功就馬上重讀清單', async () => {
        state.folders = listed();
        revoking.revoke.mockResolvedValueOnce(done({ name: 'Drafts', writable: false }));
        render(<LocalHelperFolders status={ONLINE} />);
        fireEvent.click(screen.getByRole('button', { name: '把「Drafts」改成只能讀' }));
        expect(screen.getByText('確定把「Drafts」改成只能讀？代理人就只能看、不能改；要改回請到小幫手主視窗。')).toBeDefined();
        await act(async () => {
            fireEvent.click(screen.getByRole('button', { name: '確定' }));
        });
        expect(revoking.revoke).toHaveBeenCalledWith({ name: 'Drafts', path: 'E:\\Drafts' }, 'read_only');
        expect(state.refreshWith.at(-1)).toBe(1);
    });

    it('沒有成功就不重讀', async () => {
        state.folders = listed();
        revoking.revoke.mockResolvedValueOnce(failed('not_found'));
        render(<LocalHelperFolders status={ONLINE} />);
        fireEvent.click(screen.getByRole('button', { name: '拿掉「Notes」' }));
        await act(async () => {
            fireEvent.click(screen.getByRole('button', { name: '確定' }));
        });
        expect(revoking.revoke).toHaveBeenCalledWith({ name: 'Notes', path: 'D:\\Example\\Notes' }, 'remove');
        expect(state.refreshWith.every((refresh) => refresh === 0)).toBe(true);
    });

    it('送出中按鈕停用', () => {
        state.folders = listed();
        revoking.pending = true;
        render(<LocalHelperFolders status={ONLINE} />);
        expect((screen.getByRole('button', { name: '拿掉「Notes」' }) as HTMLButtonElement).disabled).toBe(true);
    });

    it.each([
        [done({ removed: true }), '已從開放名單拿掉「Notes」。'],
        [done({ name: 'Drafts', writable: false }), '已把「Drafts」改成只能看。'],
        [failed('not_found'), '名單已經變了，沒有改。請重新整理。'],
        [failed('helper_exposed'), '小幫手為了安全停止服務中，沒有改。'],
        [failed('bad_signature'), '小幫手驗不過 Touka 的章，沒有改。請重新產生配對碼。'],
        [failed('needs_new_pairing'), '這個配對沒有鑰匙，沒有改。請重新產生配對碼。'],
        [failed('io_error'), '小幫手存不了名單，沒有改（看小幫手主視窗）。'],
        [failed('helper_offline'), '小幫手沒有連線，沒有改。'],
        [failed('helper_busy'), '小幫手正忙，沒有改。請稍後再試。'],
        [failed('timed_out'), '還沒交給小幫手，沒有改。'],
        [failed('pairing_changed'), '還沒交給小幫手，沒有改。'],
        [failed('result_unknown'), '不確定有沒有改，請看清單或小幫手主視窗。'],
        [failed('bad_result'), '不確定有沒有改，請看清單或小幫手主視窗。'],
        [failed('something_new'), '不確定有沒有改，請看清單或小幫手主視窗。'],
        [{ kind: 'denied', code: 'LOCAL_HELPER_NOT_PAIRED_HUMAN' }, '只有產生配對碼的人可以改，沒有改。'],
        [{ kind: 'denied', code: null }, '沒有權限，沒有改。'],
        [{ kind: 'failed' }, '不確定有沒有改，請看清單或小幫手主視窗。'],
    ] as [RevokeOutcome, string][])('結果 %j', (outcome, text) => {
        state.folders = listed();
        revoking.outcome = outcome;
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByText(text)).toBeDefined();
    });
});
