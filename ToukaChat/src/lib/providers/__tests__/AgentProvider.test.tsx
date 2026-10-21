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

import { fetchTouka } from '@/lib/connections';
import { AgentProvider, useAgents } from '../AgentProvider';

const mockFetchTouka = fetchTouka as ReturnType<typeof vi.fn>;

const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(AgentProvider, null, children);

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
