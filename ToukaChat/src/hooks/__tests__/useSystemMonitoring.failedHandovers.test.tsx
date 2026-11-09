import { describe, it, expect, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { fetchTouka } from '@/lib/connections';
import { useSystemMonitoring } from '../useSystemMonitoring';

vi.mock('@/lib/connections', () => ({ getActiveProxyUrl: () => 'http://x', fetchTouka: vi.fn() }));

describe('useSystemMonitoring 交接失敗（ADR-0307）', () => {
  it('把 /v1/stats 的 failed_handovers 帶到畫面', async () => {
    const failed = [{ agent: 'Alice', old_session: 'old', failures: 3 }];
    vi.mocked(fetchTouka).mockImplementation(async (_url, path) =>
      new Response(JSON.stringify(path === '/v1/stats' ? { failed_handovers: failed } : {})));
    const { result } = renderHook(() => useSystemMonitoring(true));
    await waitFor(() => expect(result.current.failedHandovers).toEqual(failed));
  });
});
