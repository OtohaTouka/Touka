import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import '@/lib/i18n';

type Outcome =
    | { kind: 'result'; result: { status: string; code: string | null; name: string | null; writable: boolean | null } }
    | { kind: 'denied'; code: string | null }
    | { kind: 'invalid' }
    | { kind: 'failed' }
    | { kind: 'timeout' };

const request = {
    pending: false,
    outcome: null as Outcome | null,
    requestOpen: vi.fn(async (_path: string) => undefined),
};
vi.mock('@/hooks/settings/useLocalOpenRequest', () => ({ useLocalOpenRequest: () => request }));

import { LocalHelperOpenFolder } from '../LocalHelperOpenFolder';

const ONLINE = { paired: true, online: true, helper_version: '0.2.0' };

function refused(code: string): Outcome {
    return { kind: 'result', result: { status: 'refused', code, name: null, writable: null } };
}

describe('LocalHelperOpenFolder（ADR-0320）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        request.pending = false;
        request.outcome = null;
    });

    it.each([
        { paired: false, online: false, helper_version: null },
        { paired: true, online: false, helper_version: null },
    ])('沒配對或小幫手沒開就不顯示', (status) => {
        const { container } = render(<LocalHelperOpenFolder status={status} />);
        expect(container.textContent).toBe('');
    });

    it('等待中或已有結果時，小幫手斷線也繼續顯示', () => {
        const offline = { paired: true, online: false, helper_version: null };
        request.pending = true;
        const { unmount } = render(<LocalHelperOpenFolder status={offline} />);
        expect(screen.getByText('請到小幫手的視窗查看並決定（2 分鐘內）。')).toBeDefined();
        unmount();
        request.pending = false;
        request.outcome = refused('no_decision');
        render(<LocalHelperOpenFolder status={offline} />);
        expect(screen.getByText('2 分鐘內沒有在小幫手的視窗決定，沒有開放。')).toBeDefined();
    });

    it('貼上路徑送出', () => {
        render(<LocalHelperOpenFolder status={ONLINE} />);
        fireEvent.change(screen.getByLabelText('資料夾路徑'), { target: { value: '"D:\\相片"' } });
        fireEvent.click(screen.getByRole('button', { name: '請小幫手開放' }));
        expect(request.requestOpen).toHaveBeenCalledWith('"D:\\相片"');
    });

    it('沒有路徑時不能送', () => {
        render(<LocalHelperOpenFolder status={ONLINE} />);
        expect((screen.getByRole('button', { name: '請小幫手開放' }) as HTMLButtonElement).disabled).toBe(true);
    });

    it('等待中按鈕停用，提醒去小幫手視窗', () => {
        request.pending = true;
        render(<LocalHelperOpenFolder status={ONLINE} />);
        fireEvent.change(screen.getByLabelText('資料夾路徑'), { target: { value: 'D:\\相片' } });
        expect((screen.getByRole('button', { name: '請小幫手開放' }) as HTMLButtonElement).disabled).toBe(true);
        expect(screen.getByText('請到小幫手的視窗查看並決定（2 分鐘內）。')).toBeDefined();
    });

    it.each([
        [
            { kind: 'result', result: { status: 'opened', code: null, name: '相片', writable: false } },
            '已開放「相片」（只能看）。',
        ],
        [
            { kind: 'result', result: { status: 'opened', code: null, name: '相片', writable: true } },
            '已開放「相片」（可以改）。',
        ],
        [refused('not_opened'), '小幫手沒有開放這個資料夾，原因寫在小幫手的視窗。'],
        [refused('no_decision'), '2 分鐘內沒有在小幫手的視窗決定，沒有開放。'],
        [refused('not_openable'), '核准後還是沒辦法開放，原因寫在小幫手的視窗。'],
        [refused('open_paused'), '小幫手暫停接收開放請求，約 10 分鐘後再試，或在小幫手主視窗按「現在恢復」。'],
        [refused('bad_signature'), '小幫手驗不過 Touka 的章，請重新產生配對碼。'],
        [refused('needs_new_pairing'), '這個配對沒有鑰匙，請重新產生配對碼。'],
        [refused('bad_request'), '小幫手版本太舊，請更新小幫手。'],
        [refused('not_supported'), '小幫手版本太舊，請更新小幫手。'],
        [
            { kind: 'result', result: { status: 'error', code: 'result_unknown', name: null, writable: null } },
            '可能已經開放，請到小幫手的視窗看名單。',
        ],
        [
            { kind: 'result', result: { status: 'error', code: 'bad_result', name: null, writable: null } },
            '可能已經開放，請到小幫手的視窗看名單。',
        ],
        [refused('approval_busy'), '小幫手正在等你決定另一個視窗，先處理那一個。'],
        [refused('approval_cooldown'), '剛關掉一個視窗，請稍等幾秒再送。'],
        [
            { kind: 'result', result: { status: 'error', code: 'helper_offline', name: null, writable: null } },
            '小幫手沒有開著。',
        ],
        [refused('helper_busy'), '小幫手正在處理太多事情，請稍後再試。'],
        [refused('helper_exposed'), '小幫手為了安全停止服務，請看小幫手的視窗。'],
        [
            { kind: 'result', result: { status: 'error', code: 'pairing_changed', name: null, writable: null } },
            '配對換了，請重新送出。',
        ],
        [
            { kind: 'result', result: { status: 'error', code: 'timed_out', name: null, writable: null } },
            '小幫手沒有收到請求，請確認它開著，再送一次。',
        ],
        [
            { kind: 'result', result: { status: 'error', code: 'io_error', name: null, writable: null } },
            '小幫手的開放名單存不進去，沒有開放。',
        ],
        [refused('something_new'), '小幫手沒有開放（something_new）。'],
        [{ kind: 'denied', code: 'LOCAL_HELPER_NOT_PAIRED_HUMAN' }, '只有產生配對碼的人可以送出。'],
        [{ kind: 'denied', code: 'LOCAL_HELPER_NOT_PAIRED' }, '還沒配對。'],
        [{ kind: 'denied', code: 'AUTH_PERMISSION_DENIED' }, '沒有權限送出。'],
        [{ kind: 'denied', code: null }, '沒有權限送出。'],
        [{ kind: 'invalid' }, '路徑裡有不能用的字元，或太長。'],
        [{ kind: 'failed' }, '沒有收到結果。如果小幫手跳出視窗，請在那邊決定；沒有的話再試一次。'],
        [{ kind: 'timeout' }, '可能已經開放，請到小幫手的視窗看名單。'],
    ] as [Outcome, string][])('結果 %j', (outcome, text) => {
        request.outcome = outcome;
        render(<LocalHelperOpenFolder status={ONLINE} />);
        expect(screen.getByText(text)).toBeDefined();
    });
});
