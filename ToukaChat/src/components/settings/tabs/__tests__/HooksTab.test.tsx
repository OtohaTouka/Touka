/**
 * ADR-0292：設定畫面的「Hooks」分頁——依伺服器給的事件顯示、編輯後送出整份設定、顯示存檔被拒的原因。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import type { HooksConfig, HooksConfigResponse } from '@/lib/types';
import type { HookSaveResult } from '@/hooks/settings/useHooksSettings';
import { HooksTab } from '../HooksTab';

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
import { toast } from 'sonner';

const state: {
    data: HooksConfigResponse | null;
    loadFailed: boolean;
    saveConfig: ReturnType<typeof vi.fn>;
    fetchConfig: ReturnType<typeof vi.fn>;
} = { data: null, loadFailed: false, saveConfig: vi.fn(), fetchConfig: vi.fn() };

vi.mock('@/hooks/settings/useHooksSettings', () => ({
    useHooksSettings: () => ({
        data: state.data, runs: [], loading: false, loadFailed: state.loadFailed, saving: false,
        fetchConfig: state.fetchConfig, fetchRuns: vi.fn(), saveConfig: state.saveConfig,
    }),
}));

function serve(config: HooksConfig, health: HooksConfigResponse['health'] = { status: 'ok', revision: 'r1' }) {
    state.data = {
        config, health,
        wired_events: ['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop'],
        matcher_events: ['PreToolUse', 'PostToolUse'],
    };
}

function section(event: string): HTMLElement {
    return screen.getByText(event).closest('section') as HTMLElement;
}

function saved(): HooksConfig {
    return state.saveConfig.mock.calls.at(-1)?.[0];
}

describe('HooksTab（ADR-0292）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        state.loadFailed = false;
        state.saveConfig = vi.fn().mockResolvedValue({ kind: 'saved' } satisfies HookSaveResult);
        serve({ enabled: false, hooks: {} });
    });

    it('每個已接線的事件一區；只有看工具名稱的事件有比對規則', () => {
        serve({
            enabled: true,
            hooks: {
                PreToolUse: [{ matcher: 'bash', hooks: [{ type: 'command', command: '/x', args: [], enabled: true, timeout: 30 }] }],
                Stop: [{ matcher: '', hooks: [{ type: 'http', url: 'https://h.test', headers: {}, enabled: true, timeout: 30 }] }],
            },
        });
        render(<HooksTab />);
        for (const event of ['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop']) {
            expect(screen.getByText(event)).toBeTruthy();
        }
        expect(within(section('PreToolUse')).getByLabelText('比對規則')).toHaveProperty('value', 'bash');
        expect(within(section('Stop')).queryByLabelText('比對規則')).toBeNull();
        expect(within(section('Stop')).getByLabelText('網址')).toHaveProperty('value', 'https://h.test');
    });

    it('沒改過不能存；打開開關後存出整份設定', async () => {
        render(<HooksTab />);
        const save = screen.getByRole('button', { name: /儲存/ });
        expect(save).toHaveProperty('disabled', true);

        fireEvent.click(screen.getByRole('switch', { name: '啟用 Hooks' }));
        fireEvent.click(save);
        await waitFor(() => expect(state.saveConfig).toHaveBeenCalledTimes(1));
        expect(saved()).toEqual({ enabled: true, hooks: {} });
        await waitFor(() => expect(toast.success).toHaveBeenCalled());
    });

    it('新增一組 command hook、填程式與參數（一行一個）', async () => {
        render(<HooksTab />);
        const pre = section('PreToolUse');
        fireEvent.click(within(pre).getByRole('button', { name: /新增一組/ }));
        fireEvent.change(within(pre).getByLabelText('比對規則'), { target: { value: 'bash|edit' } });
        fireEvent.change(within(pre).getByLabelText('程式的絕對路徑'), { target: { value: '/opt/hooks/python' } });
        fireEvent.change(within(pre).getByLabelText('參數'), { target: { value: 'guard.py\n--strict\n' } });
        fireEvent.click(screen.getByRole('button', { name: /儲存/ }));

        await waitFor(() => expect(state.saveConfig).toHaveBeenCalledTimes(1));
        expect(saved().hooks?.PreToolUse).toEqual([{
            matcher: 'bash|edit',
            hooks: [{ type: 'command', command: '/opt/hooks/python', args: ['guard.py', '--strict'], enabled: true, timeout: 30 }],
        }]);
    });

    it('在一組裡加 http hook、寫標頭；刪掉最後一個 hook 時整組拿掉', async () => {
        render(<HooksTab />);
        const stop = section('Stop');
        fireEvent.click(within(stop).getByRole('button', { name: /新增一組/ }));
        fireEvent.click(within(stop).getByRole('button', { name: /HTTP/ }));
        fireEvent.change(within(stop).getByLabelText('網址'), { target: { value: 'https://audit.test/hook' } });
        fireEvent.change(within(stop).getByLabelText('標頭'), { target: { value: 'X-Team: blue' } });
        fireEvent.click(within(stop).getAllByRole('button', { name: '刪除這個 hook' })[0]);
        fireEvent.click(screen.getByRole('button', { name: /儲存/ }));

        await waitFor(() => expect(state.saveConfig).toHaveBeenCalledTimes(1));
        expect(saved().hooks?.Stop).toEqual([{
            matcher: '',
            hooks: [{ type: 'http', url: 'https://audit.test/hook', headers: { 'X-Team': 'blue' }, enabled: true, timeout: 30 }],
        }]);

        fireEvent.click(within(stop).getByRole('button', { name: '刪除這個 hook' }));
        fireEvent.click(screen.getByRole('button', { name: /儲存/ }));
        await waitFor(() => expect(state.saveConfig).toHaveBeenCalledTimes(2));
        expect(saved().hooks).toEqual({});
    });

    it('路徑被拒收時列出每一項與原因', async () => {
        state.saveConfig = vi.fn().mockResolvedValue({
            kind: 'path_rejected',
            problems: [{ event: 'PreToolUse', where: 'args[0]', reason: 'agent_writable', value: '/data/evil.py' }],
        } satisfies HookSaveResult);
        render(<HooksTab />);
        fireEvent.click(screen.getByRole('switch', { name: '啟用 Hooks' }));
        fireEvent.click(screen.getByRole('button', { name: /儲存/ }));
        expect(await screen.findByText('PreToolUse 的 args[0]：在代理人寫得到的地方（/data/evil.py）')).toBeTruthy();
        expect(toast.success).not.toHaveBeenCalled();
    });

    it.each([
        ['conflict', { kind: 'conflict' }, 'hook 設定已被別人改過，請重新載入後再存。'],
        ['invalid', { kind: 'invalid', where: 'config.hooks.Stop', message: 'bad url' }, '欄位不合規則：config.hooks.Stop：bad url'],
    ] as const)('存檔 %s 時顯示原因', async (_name, result, text) => {
        state.saveConfig = vi.fn().mockResolvedValue(result);
        render(<HooksTab />);
        fireEvent.click(screen.getByRole('switch', { name: '啟用 Hooks' }));
        fireEvent.click(screen.getByRole('button', { name: /儲存/ }));
        expect(await screen.findByText(text)).toBeTruthy();
    });

    it('設定檔壞掉時提醒目前用的是上一份好的設定', () => {
        serve({ enabled: true, hooks: {} }, { status: 'error', revision: 'bad', detail: 'JSONDecodeError' });
        render(<HooksTab />);
        expect(screen.getByText(/JSONDecodeError/).textContent).toContain('上一份好的設定');
    });

    it('讀不到設定時只顯示讀取失敗', () => {
        state.data = null;
        state.loadFailed = true;
        render(<HooksTab />);
        expect(screen.getByText('讀不到 Hook 設定。')).toBeTruthy();
        expect(screen.queryByRole('button', { name: /儲存/ })).toBeNull();
    });

    it('重新載入會重新讀取', () => {
        render(<HooksTab />);
        fireEvent.click(screen.getByRole('button', { name: /重新載入/ }));
        expect(state.fetchConfig).toHaveBeenCalledTimes(1);
    });
});
