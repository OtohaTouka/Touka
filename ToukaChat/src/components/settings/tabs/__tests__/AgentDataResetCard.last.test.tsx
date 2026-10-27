import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import i18n from '@/lib/i18n';
import type { fetchTouka } from '@/lib/connections';
import type { AgentResetResponse, LastResetStatus } from '@/lib/types';
import { toast } from 'sonner';
import { errorMsg } from '@/lib/error_handler';
import { formatToTaipeiTime } from '@/lib/time_utils';

const LAST = '/v1/admin/agents/reset/last';
const FINISHED = 1767225600; // 2026/1/1 08:00（台北），伺服器給的是 UNIX 秒
const STARTED = FINISHED - 90;

type Reply = () => Promise<Response>;
let resetReply: Reply;
let lastReply: Reply;

const mockFetch = vi.fn((...[_u, path]: Parameters<typeof fetchTouka>) => {
    if (path === '/v1/models') {
        return Promise.resolve(Response.json({ data: [{ id: 'jin-soul', name: 'Jin', is_agent: true }] }));
    }
    if (path === LAST) return lastReply();
    if (path.startsWith('/v1/admin/agents/')) return resetReply();
    return Promise.resolve(new Response(null, { status: 404 }));
});

vi.mock('@/lib/connections', () => ({
    getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
    fetchTouka: (...args: Parameters<typeof fetchTouka>) => mockFetch(...args),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { AgentDataResetCard } from '../AgentDataResetCard';

function report(overrides: Partial<AgentResetResponse> = {}): AgentResetResponse {
    return {
        agent_id: 'jin-soul', clean: true, backup: { status: 'success', message: 'ok', filename: 'Touka_Backup_r.zip' },
        stores: [{ name: 'memory', purged: 3, remaining: 0, error: '' }],
        retained: [{ name: 'legacy', reason: '...' }], scrub: null, ...overrides,
    };
}

function status(overrides: Partial<LastResetStatus> = {}): LastResetStatus {
    return {
        state: 'done', agent_id: 'jin-soul', started_at: STARTED, finished_at: FINISHED,
        error_code: null, report: report(), ...overrides,
    };
}

function lastIs(body: LastResetStatus): void {
    lastReply = () => Promise.resolve(Response.json(body));
}

function lastCalls(): number {
    return mockFetch.mock.calls.filter((c) => c[1] === LAST).length;
}

// Intl 在日期與時間之間放細空格（U+2009）；Testing Library 比對前會把畫面上的空白收成一格
const time = (seconds: number) => formatToTaipeiTime(seconds * 1000).replace(/\s+/g, ' ');
const finishedHeading = (id = 'jin-soul') => i18n.t('settings.agent_reset.last.heading_finished', { id, time: time(FINISHED) });

/** 送出後斷線：停在「結果不明」. */
async function reachUnknown() {
    resetReply = () => Promise.reject(new TypeError('Failed to fetch'));
    render(<AgentDataResetCard />);
    fireEvent.click(await screen.findByRole('button', { name: /重置資料/ }));
    fireEvent.change(await screen.findByPlaceholderText('jin-soul'), { target: { value: 'jin-soul' } });
    fireEvent.change(screen.getByPlaceholderText(/你的帳號密碼/), { target: { value: 'pw' } });
    fireEvent.click(screen.getByRole('button', { name: /確認重置/ }));
    await screen.findByText(i18n.t('settings.agent_reset.result_unknown'));
}

function viewResult() {
    fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.view_result') }));
}

describe('AgentDataResetCard：最後一次重置的結果（丙-5）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        resetReply = () => Promise.resolve(Response.json(report()));
        lastIs({ state: 'idle', agent_id: null, started_at: null, finished_at: null, error_code: null, report: null });
    });

    it('V12 掛載時查一次，寫明是哪個靈魂、什麼時候結束', async () => {
        lastIs(status());
        render(<AgentDataResetCard />);
        expect(await screen.findByText(finishedHeading())).toBeDefined();
        expect(screen.getByText(i18n.t('settings.agent_reset.last.done'))).toBeDefined();
        expect(lastCalls()).toBe(1);
    });

    it('V13 伺服器沒有紀錄（idle）或查詢失敗：不顯示', async () => {
        render(<AgentDataResetCard />);
        await screen.findByRole('button', { name: /重置資料/ });
        await waitFor(() => expect(lastCalls()).toBe(1));
        expect(screen.queryByText(/上次重置/)).toBeNull();
        lastReply = () => Promise.reject(new TypeError('Failed to fetch'));
        render(<AgentDataResetCard />);
        await waitFor(() => expect(lastCalls()).toBe(2));
        expect(screen.queryByText(/上次重置/)).toBeNull();
    });

    it('V14 「結果不明」按查看結果：同一個靈魂已結束就顯示那份報告與時間，不跳通知', async () => {
        await reachUnknown();
        lastIs(status({ report: report({ stores: [{ name: 'sessions', purged: 7, remaining: 0, error: '' }] }) }));
        viewResult();
        expect(await screen.findByText(
            i18n.t('settings.agent_reset.last.done_header', { id: 'jin-soul', time: time(FINISHED) }),
        )).toBeDefined();
        expect(screen.getByText(new RegExp(i18n.t('settings.agent_reset.store_line', { purged: 7, remaining: 0 })))).toBeDefined();
        expect(screen.queryByText(i18n.t('settings.agent_reset.result_unknown'))).toBeNull();
        expect(toast.success).not.toHaveBeenCalled();
        expect(toast.error).not.toHaveBeenCalled();
    });

    it('V15 還在跑：說還在執行與開始時間，畫面仍是結果不明', async () => {
        await reachUnknown();
        lastIs(status({ state: 'running', finished_at: null, report: null }));
        viewResult();
        const dialog = within(await screen.findByRole('dialog'));
        expect(await dialog.findByText(
            i18n.t('settings.agent_reset.last.heading_running', { id: 'jin-soul', time: time(STARTED) }),
        )).toBeDefined();
        expect(dialog.getByText(i18n.t('settings.agent_reset.last.running'))).toBeDefined();
        expect(dialog.getByText(i18n.t('settings.agent_reset.result_unknown'))).toBeDefined();
    });

    it.each([
        ['快照失敗', 'SYSTEM_BACKUP_FAILED', () => i18n.t('settings.agent_reset.backup_failed')],
        ['看不懂的代碼', 'NOT_A_CODE', () => errorMsg('SYSTEM_ERROR')],
    ])('V16 沒有完成（%s）：照錯誤碼說原因', async (_name, code, reason) => {
        await reachUnknown();
        lastIs(status({ state: 'failed', error_code: code, report: null }));
        viewResult();
        const dialog = within(await screen.findByRole('dialog'));
        expect(await dialog.findByText(i18n.t('settings.agent_reset.last.failed', { reason: reason() }))).toBeDefined();
    });

    it('V16b 中途中斷：說可能只清了一部分', async () => {
        await reachUnknown();
        lastIs(status({ state: 'interrupted', report: null }));
        viewResult();
        const dialog = within(await screen.findByRole('dialog'));
        expect(await dialog.findByText(i18n.t('settings.agent_reset.last.interrupted'))).toBeDefined();
    });

    it.each([
        ['伺服器重啟過（idle）', () => lastIs({ state: 'idle', agent_id: null, started_at: null, finished_at: null, error_code: null, report: null })],
        ['是別的靈魂的結果', () => lastIs(status({ agent_id: 'other-soul', report: report({ agent_id: 'other-soul' }) }))],
        ['查詢失敗', () => { lastReply = () => Promise.reject(new TypeError('Failed to fetch')); }],
    ])('V17 查不到這次的結果（%s）', async (_name, arrange) => {
        await reachUnknown();
        arrange();
        viewResult();
        expect(await screen.findByText(i18n.t('settings.agent_reset.last.none'))).toBeDefined();
        expect(screen.queryByText(/上次重置：jin-soul/)).toBeNull();
    });

    it('V18 卡上的查看結果打開那份報告', async () => {
        lastIs(status({ report: report({ stores: [{ name: 'sessions', purged: 5, remaining: 1, error: '' }], clean: false }) }));
        render(<AgentDataResetCard />);
        await screen.findByText(finishedHeading());
        viewResult();
        expect(await screen.findByText(
            i18n.t('settings.agent_reset.last.done_header', { id: 'jin-soul', time: time(FINISHED) }),
        )).toBeDefined();
        expect(screen.getByText(i18n.t('settings.agent_reset.result_partial'))).toBeDefined();
        expect(screen.queryByRole('button', { name: /確認重置/ })).toBeNull();
    });

    it('V18b 那次有勾清除：打開的報告照樣列出清除結果', async () => {
        const scrub: NonNullable<AgentResetResponse['scrub']> = {
            requested: true, ran: true, complete: true, reason: null, own_snapshot: 'Touka_Backup_r.zip',
            memory_remaining: 0, leftovers_removed: 0,
            categories: (['leftovers', 'live', 'upgrade_backups', 'snapshots'] as const).map((name) => ({ name, state: 'done', reason: null })),
            items: [{ kind: 'live_store', name: 'core_memory.db', status: 'scrubbed', reason: null, facts_removed: 0, collection_removed: false, members_dropped: 0, count: null }],
        };
        lastIs(status({ report: report({ scrub }) }));
        render(<AgentDataResetCard />);
        await screen.findByText(finishedHeading());
        viewResult();
        expect(await screen.findByText(i18n.t('settings.agent_reset.scrub.summary_complete'))).toBeDefined();
    });

    it('V19 重置完成按「完成」後重新查一次', async () => {
        render(<AgentDataResetCard />);
        fireEvent.click(await screen.findByRole('button', { name: /重置資料/ }));
        fireEvent.change(await screen.findByPlaceholderText('jin-soul'), { target: { value: 'jin-soul' } });
        fireEvent.change(screen.getByPlaceholderText(/你的帳號密碼/), { target: { value: 'pw' } });
        fireEvent.click(screen.getByRole('button', { name: /確認重置/ }));
        lastIs(status());
        fireEvent.click(await screen.findByRole('button', { name: i18n.t('settings.agent_reset.done') }));
        expect(await screen.findByText(finishedHeading())).toBeDefined();
        expect(lastCalls()).toBe(2);
    });
});
