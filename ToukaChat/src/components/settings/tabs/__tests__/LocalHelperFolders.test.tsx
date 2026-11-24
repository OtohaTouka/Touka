import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import '@/lib/i18n';

type Folders =
    | { kind: 'loading' }
    | { kind: 'denied' }
    | { kind: 'failed' }
    | { kind: 'loaded'; list: Record<string, unknown> };

const state: { folders: Folders; enabledWith: boolean[] } = {
    folders: { kind: 'loading' },
    enabledWith: [],
};
vi.mock('@/hooks/settings/useLocalHelperFolders', () => ({
    useLocalHelperFolders: (enabled: boolean) => {
        state.enabledWith.push(enabled);
        return state.folders;
    },
}));

import { LocalHelperFolders } from '../LocalHelperFolders';

const ONLINE = { paired: true, online: true, helper_version: '0.5.0', version_problem: null };

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

describe('LocalHelperFolders（ADR-0330）', () => {
    beforeEach(() => {
        state.folders = { kind: 'loading' };
        state.enabledWith = [];
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

    it('小幫手被看見時警告', () => {
        state.folders = listed({ exposed: true });
        render(<LocalHelperFolders status={ONLINE} />);
        expect(screen.getByText(/為了安全，小幫手停止服務中/)).toBeDefined();
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
});
