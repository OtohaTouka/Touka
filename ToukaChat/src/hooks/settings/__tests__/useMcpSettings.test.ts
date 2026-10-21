import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://x' }));
vi.mock('@/lib/error_handler', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/error_handler')>()),
  logSSOTError: vi.fn(),
  errorMsg: (c: string) => c,
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));

import { fetchTouka } from '@/lib/connections';
import { toast } from 'sonner';
import { useMcpSettings } from '../useMcpSettings';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;

describe('useMcpSettings 隔離重啟', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockReset();
  });
  const ack = {
    isolation_ack: 'executor' as const,
    features: ['fs', 'signal'],
    generation: 7,
    bound: 'a'.repeat(64),
  };

  it('等待有專屬提示，刷新清單且解除忙碌', async () => {
    mockFetch
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: 'waiting' })),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ servers: [] })));
    const { result } = renderHook(() => useMcpSettings());
    await act(async () => {
      expect(await result.current.restartMcpServer('files')).toBe(false);
    });
    expect(toast.warning).toHaveBeenCalledWith(
      '「files」仍在等待隔離環境，請查看伺服器的隔離狀態與恢復方式。',
    );
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
    expect(mockFetch).toHaveBeenLastCalledWith('http://x', '/v1/mcp/list');
    expect(result.current.restartingKeys.size).toBe(0);
  });

  it('手動啟動送完整同意，後續一般重啟不沿用', async () => {
    mockFetch.mockImplementation(
      async (_url, path) =>
        new Response(
          JSON.stringify(
            path.endsWith('/restart')
              ? { status: 'restarted' }
              : { servers: [] },
          ),
        ),
    );
    const { result } = renderHook(() => useMcpSettings());
    await act(async () => {
      await result.current.restartMcpServer('a/b', ack);
    });
    expect(mockFetch).toHaveBeenCalledWith(
      'http://x',
      '/v1/mcp/a%2Fb/restart',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(ack),
      },
    );
    mockFetch.mockClear();
    await act(async () => {
      await result.current.restartMcpServer('a/b');
    });
    expect(mockFetch.mock.calls[0][2]).toEqual({ method: 'POST' });
  });

  it.each([
    [
      'mcp_not_waiting_isolation',
      '這台伺服器現在沒有在等隔離環境，不需要手動啟動。',
    ],
    ['mcp_isolation_ack_mismatch', '啟動方式已經改變，請重新確認後再啟動。'],
    ['mcp_entry_changed', '這台伺服器的設定剛被改過，請重新確認後再試一次。'],
  ])('拒絕 %s 翻譯並刷新，不重送舊同意', async (message, expected) => {
    mockFetch
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: { message } }), { status: 422 }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ servers: [] })));
    const { result } = renderHook(() => useMcpSettings());
    await act(async () => {
      expect(await result.current.restartMcpServer('files', ack)).toBe(false);
    });
    expect(toast.error).toHaveBeenCalledExactlyOnceWith(expected);
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(mockFetch).toHaveBeenLastCalledWith('http://x', '/v1/mcp/list');
    expect(result.current.restartingKeys.size).toBe(0);
  });
});

describe('useMcpSettings 重啟忙碌提示', () => {
  beforeEach(() => vi.clearAllMocks());

  it('busy 顯示專屬翻譯、刷新清單並解除重啟中狀態', async () => {
    mockFetch
      .mockResolvedValueOnce({ ok: true, json: async () => ({ status: 'busy' }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ servers: [] }) });
    const { result } = renderHook(() => useMcpSettings());
    let ok: boolean | undefined;
    await act(async () => { ok = await result.current.restartMcpServer('files'); });
    expect(ok).toBe(false);
    expect(toast.warning).toHaveBeenCalledWith('「files」正在重新連線，請稍後再試。');
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
    expect(mockFetch).toHaveBeenLastCalledWith('http://x', '/v1/mcp/list');
    expect(result.current.restartingKeys.has('files')).toBe(false);
  });
});

describe('useMcpSettings 失敗回饋（P2）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('saveMcpConfig 非 ok → toast.error(CONFIG_SAVE_FAILED) 且回 false', async () => {
    mockFetch.mockResolvedValue({ ok: false });
    const { result } = renderHook(() => useMcpSettings());
    let ok = true;
    await act(async () => { ok = await result.current.saveMcpConfig({}); });
    expect(ok).toBe(false);
    expect(toast.error).toHaveBeenCalled();
  });

  it('saveMcpConfig 成功 → 回 true 且不跳 error toast', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => ({ servers: [] }) });
    const { result } = renderHook(() => useMcpSettings());
    let ok = false;
    await act(async () => { ok = await result.current.saveMcpConfig({}); });
    expect(ok).toBe(true);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('syncMcpHub 非 ok → toast.error(CONFIG_SAVE_FAILED)', async () => {
    mockFetch.mockResolvedValue({ ok: false });
    const { result } = renderHook(() => useMcpSettings());
    await act(async () => { await result.current.syncMcpHub(); });
    expect(toast.error).toHaveBeenCalled();
  });
});

// 存檔回應的 isolation_cleared（McpConfigSaveResponse）：程式改了的伺服器，人給的額外資料夾已被清掉。
describe('useMcpSettings 存檔後的 isolation_cleared 提示', () => {
  beforeEach(() => vi.clearAllMocks());

  it('有被清掉的伺服器 → toast.warning 列出每一台', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'success', isolation_cleared: ['files', 'git'] }),
    });
    const { result } = renderHook(() => useMcpSettings());
    await act(async () => { await result.current.saveMcpConfig({}); });
    expect(toast.warning).toHaveBeenCalledTimes(1);
    const message = (toast.warning as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(message).toContain('額外允許的資料夾');
    expect(message).toContain('files');
    expect(message).toContain('git');
  });

  it('沒有被清掉的（空清單或舊後端沒帶欄位）→ 不提示', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => ({ status: 'success', isolation_cleared: [] }) });
    const { result } = renderHook(() => useMcpSettings());
    await act(async () => { await result.current.saveMcpConfig({}); });
    mockFetch.mockResolvedValue({ ok: true, json: async () => ({ status: 'success' }) });
    await act(async () => { await result.current.saveMcpConfig({}); });
    expect(toast.warning).not.toHaveBeenCalled();
  });
});
