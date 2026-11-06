/**
 * AgentProvider — 無窮渲染迴圈修復測試
 *
 * 驗證 refreshAgents 使用 useRef 追蹤 selectedAgentId，
 * 確保 useCallback deps 為空，避免 API 回傳不同 agent ID 時
 * 觸發 React Error #185（Maximum update depth exceeded）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';

vi.mock('@/lib/connections', () => ({
    fetchTouka: vi.fn(),
    getActiveProxyUrl: vi.fn().mockReturnValue('http://localhost:8000'),
    setKnownAgentIds: vi.fn(),
}));

vi.mock('@/lib/error_handler', () => ({
    logSSOTError: vi.fn(),
}));

import { fetchTouka, setKnownAgentIds } from '@/lib/connections';
import { AgentProvider, useAgents } from '../AgentProvider';

const mockFetchTouka = fetchTouka as ReturnType<typeof vi.fn>;

const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(AgentProvider, null, children);

describe('AgentProvider — agentsStatus', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    const ok = { ok: true, json: async () => [{ id: 'alice', name: 'Alice', type: 'soul', is_group: false }] };

    it('一開始是載入中，讀到變 ready', async () => {
        mockFetchTouka.mockResolvedValue(ok);
        const { result } = renderHook(() => useAgents(), { wrapper });
        expect(result.current.agentsStatus).toBe('loading');
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));
    });

    it.each([
        ['回應不是 200', () => mockFetchTouka.mockResolvedValue({ ok: false })],
        ['連線失敗', () => mockFetchTouka.mockRejectedValue(new Error('down'))],
    ])('%s 變 failed', async (_label, arrange) => {
        arrange();
        const { result } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(result.current.agentsStatus).toBe('failed'));
    });

    it.each(['touka_auth_changed', 'touka_connections_updated'])('失敗後收到 %s：重試期間載入中，成功變 ready', async (eventName) => {
        mockFetchTouka.mockResolvedValue({ ok: false });
        const { result } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(result.current.agentsStatus).toBe('failed'));

        let finish!: (value: unknown) => void;
        mockFetchTouka.mockReturnValue(new Promise(resolve => { finish = resolve; }));
        act(() => { window.dispatchEvent(new Event(eventName)); });
        expect(result.current.agentsStatus).toBe('loading');
        await act(async () => { finish(ok); });
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));
    });

    it('卸載之後才回來的舊請求：不把舊身分的靈魂寫回', async () => {
        let finish!: (value: unknown) => void;
        mockFetchTouka.mockReturnValue(new Promise(resolve => { finish = resolve; }));
        const { unmount } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());
        unmount();
        await act(async () => { finish(ok); });
        expect(setKnownAgentIds).not.toHaveBeenCalled();
    });

    it.each(['touka_auth_changed', 'touka_connections_updated'])('卸載後收到 %s 不再重抓', async (eventName) => {
        mockFetchTouka.mockResolvedValue(ok);
        const { result, unmount } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));
        unmount();
        mockFetchTouka.mockClear();
        window.dispatchEvent(new Event(eventName));
        expect(mockFetchTouka).not.toHaveBeenCalled();
    });

    it('讀到過之後，一次失敗留著舊清單，不退回 failed', async () => {
        mockFetchTouka.mockResolvedValue(ok);
        const { result } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));

        mockFetchTouka.mockResolvedValue({ ok: false });
        await act(async () => { await result.current.refreshAgents(); });
        expect(result.current.agentsStatus).toBe('ready');
        expect(result.current.agents).toHaveLength(1);
    });

    const bobOk = { ok: true, json: async () => [{ id: 'bob', name: 'Bob', type: 'soul', is_group: false }] };

    it('存連線（touka_connections_updated）：不清空、不閃載入中，新清單回來才換', async () => {
        mockFetchTouka.mockResolvedValue(ok);
        const { result } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));

        let finish!: (value: unknown) => void;
        mockFetchTouka.mockReturnValue(new Promise(resolve => { finish = resolve; }));
        act(() => { window.dispatchEvent(new Event('touka_connections_updated')); });
        expect(result.current.agentsStatus).toBe('ready');
        expect(result.current.agents.map(agent => agent.id)).toEqual(['alice']);

        await act(async () => { finish(bobOk); });
        await waitFor(() => expect(result.current.agents.map(agent => agent.id)).toEqual(['bob']));
    });

    it('存連線後重抓失敗：留著舊清單，不退回 failed', async () => {
        mockFetchTouka.mockResolvedValue(ok);
        const { result } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));

        mockFetchTouka.mockResolvedValue({ ok: false });
        await act(async () => { window.dispatchEvent(new Event('touka_connections_updated')); });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalledTimes(2));
        await act(async () => { await Promise.resolve(); });
        expect(result.current.agentsStatus).toBe('ready');
        expect(result.current.agents.map(agent => agent.id)).toEqual(['alice']);
    });

    it('讀到過之後登入狀態翻轉（touka_auth_changed）：先清空、載入中，新清單回來才換', async () => {
        mockFetchTouka.mockResolvedValue(ok);
        const { result } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));

        let finish!: (value: unknown) => void;
        mockFetchTouka.mockReturnValue(new Promise(resolve => { finish = resolve; }));
        act(() => { window.dispatchEvent(new Event('touka_auth_changed')); });
        expect(result.current.agents).toEqual([]);
        expect(result.current.agentsStatus).toBe('loading');

        await act(async () => { finish(bobOk); });
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));
        expect(result.current.agents.map(agent => agent.id)).toEqual(['bob']);
    });

    it('換帳號後讀不到：不留上一個帳號的清單', async () => {
        mockFetchTouka.mockResolvedValue(ok);
        const { result } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));

        mockFetchTouka.mockResolvedValue({ ok: false });
        act(() => { window.dispatchEvent(new Event('touka_auth_changed')); });
        await waitFor(() => expect(result.current.agentsStatus).toBe('failed'));
        expect(result.current.agents).toEqual([]);
    });

    it.each([
        ['回應成功', (finish: (value: unknown) => void) => finish(ok), () => undefined],
        ['回應失敗', (finish: (value: unknown) => void) => finish({ ok: false }), () => undefined],
        ['連線失敗', () => undefined, (fail: (reason: Error) => void) => fail(new Error('down'))],
    ])('上一個帳號還在路上的請求（%s）：不蓋掉新帳號的結果', async (_label, settle, reject) => {
        mockFetchTouka.mockResolvedValue(ok);
        const { result } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(result.current.agentsStatus).toBe('ready'));

        let finishOld!: (value: unknown) => void;
        let failOld!: (reason: Error) => void;
        mockFetchTouka.mockReturnValueOnce(new Promise((resolve, fail) => { finishOld = resolve; failOld = fail; }));
        let oldResult: Promise<boolean> | undefined;
        act(() => { oldResult = result.current.refreshAgents(); });

        mockFetchTouka.mockReturnValueOnce(new Promise<never>(() => undefined));
        act(() => { window.dispatchEvent(new Event('touka_auth_changed')); });

        await act(async () => { settle(finishOld); reject(failOld); await oldResult; });
        expect(result.current.agents).toEqual([]);
        expect(result.current.agentsStatus).toBe('loading');
    });
});

describe('AgentProvider — refreshAgents 穩定性與無窮迴圈防護', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetchTouka.mockResolvedValue({
            ok: true,
            json: async () => [],
        });
    });

    it('refreshAgents 參考在 selectedAgentId 改變後保持穩定（useRef pattern）', async () => {
        const { result } = renderHook(() => useAgents(), { wrapper });

        // 等待初始 effect 完成
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());

        const firstRef = result.current.refreshAgents;

        act(() => {
            result.current.setSelectedAgentId('different-id');
        });

        // 修復後：refreshAgents deps 為空，參考不因 selectedAgentId 改變而重建
        expect(result.current.refreshAgents).toBe(firstRef);
    });

    it('API 回傳與 selectedAgentId 不同的 agent 時，fetchTouka 只被呼叫一次（無無窮迴圈）', async () => {
        // 模擬 API 回傳 "alice"，初始 selectedAgentId 為空 → 必定觸發 setSelectedAgentId
        mockFetchTouka.mockResolvedValue({
            ok: true,
            json: async () => [
                { id: 'alice', name: 'Alice', type: 'ai', is_group: false },
            ],
        });

        const { result } = renderHook(() => useAgents(), { wrapper });

        await waitFor(() => {
            expect(result.current.agents.length).toBe(1);
        });

        // 等待任何可能的連鎖更新
        await new Promise(r => setTimeout(r, 100));

        // 核心斷言：fetchTouka 只被呼叫一次，證明無無窮迴圈
        expect(mockFetchTouka).toHaveBeenCalledTimes(1);

        // selectedAgentId 已更新為 API 回傳的第一個 agent
        expect(result.current.selectedAgentId).toBe('alice');
    });

    it('selectedAgentId 已在 API 回傳清單中時，再次 refresh 不強制改選', async () => {
        mockFetchTouka.mockResolvedValue({
            ok: true,
            json: async () => [
                { id: 'charlie', name: 'Charlie Agent', type: 'ai', is_group: false },
            ],
        });

        const { result } = renderHook(() => useAgents(), { wrapper });

        // 首次 refresh：空預設不在清單 → 自動選為 API 第一個 (charlie)
        await waitFor(() => {
            expect(result.current.selectedAgentId).toBe('charlie');
        });

        // 明確再 refresh 一次；此時 selection 已在清單中 → 不應被改動
        await act(async () => {
            await result.current.refreshAgents();
        });

        expect(result.current.selectedAgentId).toBe('charlie');
    });

    it('API 失敗時，agents 保持為空陣列且無拋出', async () => {
        mockFetchTouka.mockResolvedValue({ ok: false });

        const { result } = renderHook(() => useAgents(), { wrapper });

        await new Promise(r => setTimeout(r, 50));

        expect(result.current.agents).toEqual([]);
    });

    it.each(['success', 'http-error', 'network-error'])('刷新 %s 回傳實際結果', async (outcome) => {
        const { result } = renderHook(() => useAgents(), { wrapper });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());
        if (outcome === 'http-error') mockFetchTouka.mockResolvedValue({ ok: false });
        if (outcome === 'network-error') mockFetchTouka.mockRejectedValue(new TypeError('synthetic network failure'));
        let refreshed: unknown;
        await act(async () => { refreshed = await result.current.refreshAgents(); });
        expect(refreshed).toBe(outcome === 'success');
    });
});
