import { describe, it, expect, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { fetchTouka } from '@/lib/connections';
import { useSystemMonitoring } from '../useSystemMonitoring';

vi.mock('@/lib/connections', () => ({ getActiveProxyUrl: () => 'http://x', fetchTouka: vi.fn() }));

describe('useSystemMonitoring 快取命中率與價目表（ADR-0311）', () => {
  const answer = (stats: unknown) => vi.mocked(fetchTouka).mockImplementation(async (_url, path) =>
    new Response(JSON.stringify(path === '/v1/stats' ? stats : {})));

  it('把 /v1/stats 的 cache_hit_rates、pricing 帶到畫面', async () => {
    const rates = { Alice: { Anthropic: 0.5 } };
    const pricing = { verified_at: '2026-11-10', has_override: false, estimated_models: [] };
    answer({ cache_hit_rates: rates, pricing });
    const { result } = renderHook(() => useSystemMonitoring(true));
    await waitFor(() => expect(result.current.cacheHitRates).toEqual(rates));
    expect(result.current.pricing).toEqual(pricing);
  });

  it('舊後端沒有這兩欄時是空的', async () => {
    answer({ failed_handovers: [{ agent: 'Alice' }] });
    const { result } = renderHook(() => useSystemMonitoring(true));
    await waitFor(() => expect(result.current.failedHandovers).toHaveLength(1));
    expect(result.current.cacheHitRates).toEqual({});
    expect(result.current.pricing).toBeNull();
  });
});
