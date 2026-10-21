import { renderHook, act } from '@testing-library/react';
import { useSnsBotSettings } from '@/hooks/settings/useSnsBotSettings';
import { fetchTouka } from '@/lib/connections';
import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock the dependencies
vi.mock('@/lib/connections', () => ({
    getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
    fetchTouka: vi.fn(),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
import { toast } from 'sonner';

describe('useSnsBotSettings Hook', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        window.localStorage.clear();
        vi.mocked(fetchTouka).mockResolvedValue({
            ok: true, json: async () => ({ discord: [], telegram: [] }),
        } as Response);
    });

    it('should initialize with default empty config if localStorage is empty', () => {
        const { result } = renderHook(() => useSnsBotSettings());
        expect(result.current.snsBots).toEqual({ discord: [], telegram: [] });
    });

    it('should ignore retired localStorage configuration even when it contains a token', () => {
        const mockData = {
            discord: [{ token: 'test-token', agent_id: 'test-agent', enabled: true }],
            telegram: []
        };
        window.localStorage.setItem('touka_sns_bots', JSON.stringify(mockData));

        const { result } = renderHook(() => useSnsBotSettings());
        expect(result.current.snsBots).toEqual({ discord: [], telegram: [] });
    });

    it('should reject invalid bot configuration when saving (F1: toast, not alert)', async () => {
        const { result } = renderHook(() => useSnsBotSettings());
        await act(async () => { await result.current.fetchSnsBots(); });
        vi.mocked(fetchTouka).mockClear();

        const invalidData = {
            discord: [{ token: '', agent_id: 'test-agent', enabled: true }], // Empty token
            telegram: []
        };

        await act(async () => { expect(await result.current.saveSnsBots(invalidData)).toBe(false); });
        expect(fetchTouka).not.toHaveBeenCalled();
        // F1：改用 sonner toast.error（取代阻塞式原生 alert）。
        expect(toast.error).toHaveBeenCalled();
    });
});
