import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import i18n from '@/lib/i18n';
import type { fetchTouka } from '@/lib/connections';
import type { AgentResetResponse, MemoryScrubReport } from '@/lib/types';
import { toast } from 'sonner';
import { ERRORS } from '@/lib/error_codes';
import openapi from '../../../../../../openapi.json';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';

type Reply = () => Promise<Response>;
let resetReply: Reply;

const mockFetch = vi.fn((...[_u, path]: Parameters<typeof fetchTouka>) => {
    if (path === '/v1/models') {
        return Promise.resolve(Response.json({ data: [{ id: 'jin-soul', name: 'Jin', is_agent: true }] }));
    }
    if (path === '/v1/admin/agents/reset/last') return Promise.resolve(Response.json({ state: 'idle' }));
    if (path.startsWith('/v1/admin/agents/')) return resetReply();
    return Promise.resolve(new Response(null, { status: 404 }));
});

vi.mock('@/lib/connections', () => ({
    getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
    fetchTouka: (...args: Parameters<typeof fetchTouka>) => mockFetch(...args),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { AgentDataResetCard } from '../AgentDataResetCard';
import { AgentResetScrubResult } from '../AgentResetScrubResult';

type Item = NonNullable<MemoryScrubReport['items']>[number];
const REASON_COUNT = 29;

function item(kind: Item['kind'], name: string, status: Item['status'], reason: Item['reason'] = null): Item {
    return { kind, name, status, reason, facts_removed: 0, collection_removed: false, members_dropped: 0, count: null };
}

const SCRUB_DONE: MemoryScrubReport = {
    requested: true, ran: true, complete: true, reason: null, own_snapshot: 'Touka_Backup_r.zip',
    memory_remaining: 0, leftovers_removed: 0,
    categories: (['leftovers', 'live', 'upgrade_backups', 'snapshots'] as const).map((name) => ({ name, state: 'done', reason: null })),
    items: [
        { kind: 'live_store', name: 'core_memory.db', status: 'scrubbed', reason: null, facts_removed: 0, collection_removed: false, members_dropped: 0, count: null },
        { kind: 'snapshot', name: 'Touka_Backup_r.zip', status: 'scrubbed', reason: null, facts_removed: 2, collection_removed: true, members_dropped: 0, count: null },
    ],
};

function report(overrides: Partial<AgentResetResponse> = {}): AgentResetResponse {
    return {
        agent_id: 'jin-soul', clean: true, backup: { status: 'success', message: 'ok', filename: 'Touka_Backup_r.zip' },
        stores: [{ name: 'memory', purged: 3, remaining: 0, error: '' }],
        retained: [{ name: 'legacy', reason: '...' }], scrub: null, ...overrides,
    };
}

function replyWith(body: AgentResetResponse): void {
    resetReply = () => Promise.resolve(Response.json(body));
}

async function openDialog() {
    const view = render(<AgentDataResetCard />);
    fireEvent.click(await screen.findByRole('button', { name: /重置資料/ }));
    await screen.findByText(/重置代理人/);
    return view;
}

function fillAndSubmit() {
    fireEvent.change(screen.getByPlaceholderText('jin-soul'), { target: { value: 'jin-soul' } });
    fireEvent.change(screen.getByPlaceholderText(/你的帳號密碼/), { target: { value: 'pw' } });
    fireEvent.click(screen.getByRole('button', { name: /確認重置/ }));
}

function confirmButton(): HTMLButtonElement {
    return screen.getByRole('button', { name: /確認重置/ }) as HTMLButtonElement;
}

async function sentBody(): Promise<Record<string, unknown>> {
    let body: Record<string, unknown> = {};
    await waitFor(() => {
        const call = mockFetch.mock.calls.find((c) => String(c[1]).endsWith('/reset'));
        if (!call || typeof call[2]?.body !== 'string') throw new Error('no reset request yet');
        body = JSON.parse(call[2].body);
    });
    return body;
}

function flatten(node: unknown, prefix = '', out = new Set<string>()): Set<string> {
    for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
        const path = prefix ? `${prefix}.${key}` : key;
        if (value && typeof value === 'object') flatten(value, path, out);
        else out.add(path);
    }
    return out;
}

describe('AgentDataResetCard：清除連備份', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        replyWith(report());
    });

    it('V01 預設不勾，送出的兩個旗標都是 false', async () => {
        await openDialog();
        expect((screen.getByRole('checkbox') as HTMLButtonElement).getAttribute('aria-checked')).toBe('false');
        fillAndSubmit();
        const body = await sentBody();
        expect(body.scrub_backups).toBe(false);
        expect(body.scrub_acknowledged).toBe(false);
    });

    it('V02 勾選後要按第二段確認，確認鈕才會啟用；送出兩個 true', async () => {
        await openDialog();
        fireEvent.change(screen.getByPlaceholderText('jin-soul'), { target: { value: 'jin-soul' } });
        fireEvent.change(screen.getByPlaceholderText(/你的帳號密碼/), { target: { value: 'pw' } });
        expect(confirmButton().disabled).toBe(false);
        fireEvent.click(screen.getByRole('checkbox'));
        expect(confirmButton().disabled).toBe(true);
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.guarantee'))).toBeDefined();
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        expect(confirmButton().disabled).toBe(false);
        fireEvent.click(confirmButton());
        const body = await sentBody();
        expect(body.scrub_backups).toBe(true);
        expect(body.scrub_acknowledged).toBe(true);
    });

    it('V03 取消勾選後歸零：再勾要重新確認，不勾就送 false', async () => {
        await openDialog();
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fireEvent.click(screen.getByRole('checkbox'));
        expect(screen.queryByText(i18n.t('settings.agent_reset.scrub.acknowledged'))).toBeNull();
        fireEvent.click(screen.getByRole('checkbox'));
        expect(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') })).toBeDefined();
        fireEvent.click(screen.getByRole('checkbox'));
        fillAndSubmit();
        const body = await sentBody();
        expect([body.scrub_backups, body.scrub_acknowledged]).toEqual([false, false]);
    });

    it('V04 關閉對話框後歸零', async () => {
        await openDialog();
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fireEvent.click(screen.getByRole('button', { name: /取消/ }));
        await waitFor(() => expect(screen.queryByText(/重置代理人/)).toBeNull());
        fireEvent.click(screen.getByRole('button', { name: /重置資料/ }));
        await screen.findByText(/重置代理人/);
        expect(screen.getByRole('checkbox').getAttribute('aria-checked')).toBe('false');
        expect(screen.queryByText(i18n.t('settings.agent_reset.scrub.acknowledged'))).toBeNull();
    });

    it('V05b 完成、但有不在範圍的舊格式資料：總結句與保留提示都照實說，不說每一份都清了（丙-3）', async () => {
        const withLegacy: MemoryScrubReport = {
            ...SCRUB_DONE,
            items: [
                ...(SCRUB_DONE.items ?? []),
                { ...item('info', 'facts.jsonl', 'info', 'out_of_scope'), count: 4 },
                { ...item('info', 'core_memory.v0-to-v1.1.db', 'info', 'out_of_scope'), count: 2 },
            ],
        };
        replyWith(report({ scrub: withLegacy }));
        await openDialog();
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fillAndSubmit();
        // 數字是還留著這類資料的備份份數（每份一個資訊項），筆數在下方清單
        expect(await screen.findByText(i18n.t('settings.agent_reset.scrub.summary_complete_out_of_scope', { n: 2 }))).toBeDefined();
        expect(screen.queryByText(i18n.t('settings.agent_reset.scrub.summary_complete'))).toBeNull();
        expect(screen.getByText(i18n.t('backup_notice.still_there_scrubbed_partial', { id: 'jin-soul' }))).toBeDefined();
        expect(screen.queryByText(i18n.t('backup_notice.still_there_scrubbed', { id: 'jin-soul' }))).toBeNull();
    });

    it('V05c 線上庫又有新寫入：說又有記憶，不說有沒有整理（整理前或整理後都可能發現，丙-3）', () => {
        render(<AgentResetScrubResult report={{
            ...SCRUB_DONE, complete: false, reason: 'rewritten_after_reset', memory_remaining: 1,
            items: [item('live_store', 'core_memory.db', 'failed', 'rewritten_after_reset')],
        } as MemoryScrubReport} />);
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.live_rewritten'))).toBeDefined();
        const reason = i18n.t('settings.agent_reset.scrub.reasons.rewritten_after_reset');
        expect(screen.queryByText(i18n.t('settings.agent_reset.scrub.live_not_compacted', { reason }))).toBeNull();
    });

    it('V05 結果分類別列出、有總結句（從真實可能出現的報告）', async () => {
        const failedList: MemoryScrubReport = {
            ...SCRUB_DONE, complete: false, reason: 'list_failed',
            categories: [
                { name: 'leftovers', state: 'done', reason: null }, { name: 'live', state: 'done', reason: null },
                { name: 'upgrade_backups', state: 'done', reason: null }, { name: 'snapshots', state: 'failed', reason: 'list_failed' },
            ],
            items: [
                item('live_store', 'core_memory.db', 'scrubbed'),
                item('leftover', '.snapshot-20261015', 'failed', 'unknown_file'),
                item('upgrade_backup', 'core_memory.v0-to-v1.1.db', 'failed', 'in_use'),
                item('category', 'snapshots', 'failed', 'list_failed'),
                { ...item('info', 'facts.jsonl', 'info', 'out_of_scope'), count: 4 },
            ],
        };
        replyWith(report({ scrub: failedList }));
        await openDialog();
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fillAndSubmit();
        const listFailed = i18n.t('settings.agent_reset.scrub.reasons.list_failed');
        expect(await screen.findByText(i18n.t('settings.agent_reset.scrub.summary_unfinished', { reason: listFailed }))).toBeDefined();
        expect(screen.queryByText(/這 0 個檔|These 0 files/)).toBeNull();
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.category_failed', {
            category: i18n.t('settings.agent_reset.scrub.categories.snapshots'), reason: listFailed,
        }))).toBeDefined();
        expect(screen.getByText(new RegExp(`\.snapshot-20261015.*${i18n.t('settings.agent_reset.scrub.reasons.unknown_file')}`))).toBeDefined();
        expect(screen.getByText(new RegExp(i18n.t('settings.agent_reset.scrub.reasons.in_use')))).toBeDefined();
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.info_title'))).toBeDefined();
    });

    it.each([
        ['internal_error（四類都沒開始）', {
            complete: false, reason: 'internal_error', items: [],
            categories: (['leftovers', 'live', 'upgrade_backups', 'snapshots'] as const).map((name) => ({ name, state: 'not_started', reason: null })),
        }, 'internal_error', 4],
        ['own_snapshot_missing', { complete: false, reason: 'own_snapshot_missing' }, 'own_snapshot_missing', 0],
    ] as const)('V05 沒完成時總結句寫原因、不算檔數：%s', (_name, overrides, reason, notStarted) => {
        render(<AgentResetScrubResult report={{ ...SCRUB_DONE, ...overrides } as MemoryScrubReport} />);
        const text = i18n.t(`settings.agent_reset.scrub.reasons.${reason}`);
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.summary_unfinished', { reason: text }))).toBeDefined();
        const notStartedLines = (['leftovers', 'live', 'upgrade_backups', 'snapshots'] as const).map((name) =>
            i18n.t('settings.agent_reset.scrub.category_not_started', { category: i18n.t(`settings.agent_reset.scrub.categories.${name}`) }));
        expect(notStartedLines.filter((line) => screen.queryByText(line) !== null)).toHaveLength(notStarted);
        expect(screen.queryByText(/這 0 個檔/)).toBeNull();
    });

    it('V05 只有個別檔沒清掉時：總結句說沒完成，另一句照實寫幾個檔維持原樣', () => {
        render(<AgentResetScrubResult report={{
            ...SCRUB_DONE, complete: false, reason: null,
            items: [
                item('live_store', 'core_memory.db', 'scrubbed'),  // 線上庫不算在「檔」裡
                item('leftover', '.Touka_Backup_x.zip.memscrub-ab12cd34.tmp', 'removed'),
                item('snapshot', 'Touka_Backup_a.zip', 'scrubbed'),
                item('snapshot', 'Touka_Backup_b.zip', 'failed', 'in_use'),
            ],
        }} />);
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.summary_unfinished', {
            reason: i18n.t('settings.agent_reset.scrub.reason_see_details'),
        }))).toBeDefined();
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.files_left', { failed: 1, done: 1 }))).toBeDefined();
    });

    it('V05 每個原因代碼兩語都有翻譯鍵', () => {
        const reasons = openapi.components.schemas.ScrubItemResult.properties.reason.anyOf[0].enum as string[];
        expect(reasons.length).toBe(REASON_COUNT);
        for (const catalog of [zhTW, en]) {
            const keys = flatten(catalog);
            for (const reason of reasons) expect(keys.has(`settings.agent_reset.scrub.reasons.${reason}`), reason).toBe(true);
        }
    });

    it('V06 經 fetchTouka、帶自己的 signal（不套連線逾時）；卸載不中止', async () => {
        let release: (r: Response) => void = () => undefined;
        resetReply = () => new Promise<Response>((resolve) => { release = resolve; });
        const view = await openDialog();
        fillAndSubmit();
        await sentBody();
        const call = mockFetch.mock.calls.find((c) => String(c[1]).endsWith('/reset'));
        const signal = call?.[2]?.signal;
        expect(signal).toBeInstanceOf(AbortSignal);
        expect(call?.[2]).not.toHaveProperty('requestScope');
        expect(call?.[2]).not.toHaveProperty('nonStreaming');
        view.unmount();
        expect(signal?.aborted).toBe(false);
        release(Response.json(report()));
    });

    it('V07 斷線或逾時 → 顯示「結果不明」，不說失敗', async () => {
        resetReply = () => Promise.reject(new TypeError('Failed to fetch'));
        await openDialog();
        fillAndSubmit();
        expect(await screen.findByText(i18n.t('settings.agent_reset.result_unknown'))).toBeDefined();
        expect(toast.error).not.toHaveBeenCalled();
        expect(toast.success).not.toHaveBeenCalled();
        expect(screen.queryByText(/失敗/)).toBeNull();
    });

    it.each([
        ['504 HTML（閘道逾時）', () => new Response('<html>Gateway Time-out</html>', { status: 504 })],
        ['502 沒有錯誤碼', () => Response.json({ error: 'bad gateway' }, { status: 502 })],
        ['524 雲端逾時', () => new Response('timeout', { status: 524 })],
        ['500 沒有錯誤碼', () => new Response('Internal Server Error', { status: 500 })],
    ])('V07b %s → 結果不明，不說失敗', async (_name, reply) => {
        resetReply = () => Promise.resolve(reply());
        await openDialog();
        fillAndSubmit();
        expect(await screen.findByText(i18n.t('settings.agent_reset.result_unknown'))).toBeDefined();
        expect(toast.error).not.toHaveBeenCalled();
    });

    it('V07b 伺服器自己回的錯誤碼（例如 503 清除不可用）仍是明確拒絕', async () => {
        const { id } = ERRORS.MEMORY_SCRUB_UNAVAILABLE;
        resetReply = () => Promise.resolve(Response.json({ detail: { code: 'MEMORY_SCRUB_UNAVAILABLE', id } }, { status: 503 }));
        await openDialog();
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fillAndSubmit();
        await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
        expect(screen.queryByText(i18n.t('settings.agent_reset.result_unknown'))).toBeNull();
    });

    it('V08 勾了，但回應沒有 scrub → 顯示伺服器沒有執行清除', async () => {
        replyWith(report({ scrub: undefined }));
        await openDialog();
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fillAndSubmit();
        expect(await screen.findByText(i18n.t('settings.agent_reset.scrub.not_run'))).toBeDefined();
        expect(toast.success).not.toHaveBeenCalled();
    });

    it('V09 clean 為真但清除沒完成 → 不跳重置成功', async () => {
        replyWith(report({ scrub: { ...SCRUB_DONE, complete: false, reason: 'own_snapshot_missing' } }));
        await openDialog();
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fillAndSubmit();
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith(i18n.t('settings.agent_reset.scrub.toast_incomplete', { id: 'jin-soul' })));
        expect(toast.success).not.toHaveBeenCalled();
    });

    it('V09 沒勾、clean 為真 → 重置成功', async () => {
        await openDialog();
        fillAndSubmit();
        await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
    });

    it('V10 勾選時確認畫面換成清除版文案；結果的刻意保留照伺服器回的列', async () => {
        replyWith(report({ scrub: SCRUB_DONE, retained: [{ name: 'legacy', reason: '...' }] }));
        await openDialog();
        expect(screen.getByText(i18n.t('settings.agent_reset.wipe_list'))).toBeDefined();
        fireEvent.click(screen.getByRole('checkbox'));
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.wipe_list'))).toBeDefined();
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.keep_list'))).toBeDefined();
        expect(screen.queryByText(i18n.t('settings.agent_reset.keep_list'))).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fillAndSubmit();
        expect(await screen.findByText(i18n.t('settings.agent_reset.scrub.summary_complete'))).toBeDefined();
        expect(screen.getByText(i18n.t('settings.agent_reset.preserved_line', { items: 'legacy' }))).toBeDefined();
        expect(screen.getByText(i18n.t('settings.agent_reset.scrub.result_clean_live'))).toBeDefined();
        await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
    });

    it('V11 清除完成時改提醒對話與其他資料還在快照裡（不提升級備份與勾選）；沒完成時照樣完整提醒', async () => {
        replyWith(report({ scrub: SCRUB_DONE }));
        const view = await openDialog();
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fillAndSubmit();
        expect(await screen.findByText(i18n.t('settings.agent_reset.scrub.summary_complete'))).toBeDefined();
        expect(screen.getByText(/記憶已從備份清掉，但「jin-soul」的對話與其他資料還留在系統快照裡/)).toBeDefined();
        expect(screen.getByText(/Touka_AutoBackup_/)).toBeDefined();
        expect(screen.getByText(/刪整份快照/)).toBeDefined();
        expect(screen.queryByText(/vault\/core_memory_backups/)).toBeNull();
        expect(screen.queryByText(/建議：重置時勾選/)).toBeNull();
        view.unmount();

        replyWith(report({ scrub: { ...SCRUB_DONE, complete: false, reason: 'own_snapshot_missing' } }));
        await openDialog();
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.agent_reset.scrub.acknowledge') }));
        fillAndSubmit();
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
        expect(screen.getByText(/「jin-soul」的資料還留在系統快照裡/)).toBeDefined();
        expect(screen.getByText(/vault\/core_memory_backups/)).toBeDefined();
        expect(screen.getByText(/建議：重置時勾選/)).toBeDefined();
    });
});
