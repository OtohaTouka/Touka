import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://x' }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (c: string) => c }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { fetchTouka } from '@/lib/connections';
import { toast } from 'sonner';
import { useGroupSettings } from '../useGroupSettings';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;

describe('useGroupSettings 載入失敗回饋（P2）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('fetchWhitelist non-ok → toast.error(CONFIG_LOAD_FAILED)', async () => {
    mockFetch.mockResolvedValue({ ok: false });
    const { result } = renderHook(() => useGroupSettings());
    await act(async () => { await result.current.fetchWhitelist(); });
    expect(toast.error).toHaveBeenCalled();
  });

  it('fetchModels throw → toast.error(CONFIG_LOAD_FAILED)', async () => {
    mockFetch.mockRejectedValue(new Error('network'));
    const { result } = renderHook(() => useGroupSettings());
    await act(async () => { await result.current.fetchModels(); });
    expect(toast.error).toHaveBeenCalled();
  });
});
