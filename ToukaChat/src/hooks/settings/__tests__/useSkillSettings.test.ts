import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://x' }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (c: string) => c }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { fetchTouka } from '@/lib/connections';
import { toast } from 'sonner';
import { useSkillSettings } from '../useSkillSettings';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;

describe('useSkillSettings.exportSkill 失敗回饋（P2）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('匯出 fetch throw（離線/連不上）→ toast.error(SYSTEM_ERROR) 且回 false', async () => {
    mockFetch.mockRejectedValue(new Error('network'));
    const { result } = renderHook(() => useSkillSettings());
    let ok = true;
    await act(async () => { ok = await result.current.exportSkill({ id: 's1' } as never); });
    expect(ok).toBe(false);
    expect(toast.error).toHaveBeenCalled();
  });
});
