import { describe, it, expect, beforeEach, vi } from 'vitest';
import { getActiveHeartbeatUrl, fetchTouka, getAccessToken, clearAccessToken } from '@/lib/connections';
import { CONFIG } from '@/lib/config';

// 模擬 getLocalStorageItem 以提供自定義 proxy URL
vi.mock('@/hooks/useLocalStorage', () => ({
  getLocalStorageItem: vi.fn(),
}));

import { getLocalStorageItem } from '@/hooks/useLocalStorage';

describe('getActiveHeartbeatUrl — M3 Port & Path Remap', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('應將 localhost:8000 加上子路徑映射至 8082 且去除子路徑', () => {
    vi.mocked(getLocalStorageItem).mockReturnValue([
      { provider: 'proxy', enabled: true, url: 'http://localhost:8000/api/v1' }
    ]);
    const result = getActiveHeartbeatUrl();
    expect(result).toBe('http://localhost:8082');
  });

  it('應將 127.0.0.1:8000 映射至 8082 且去除子路徑', () => {
    vi.mocked(getLocalStorageItem).mockReturnValue([
      { provider: 'proxy', enabled: true, url: 'http://127.0.0.1:8000/some/path' }
    ]);
    const result = getActiveHeartbeatUrl();
    expect(result).toBe('http://127.0.0.1:8082');
  });

  it('非本地 localhost 且非 8000 端口時，應回傳預設 CONFIG.HEARTBEAT_URL', () => {
    vi.mocked(getLocalStorageItem).mockReturnValue([
      { provider: 'proxy', enabled: true, url: 'http://localhost:9000/api' }
    ]);
    const result = getActiveHeartbeatUrl();
    expect(result).toBe(CONFIG.HEARTBEAT_URL);
  });

  it('外部網域名稱時，應回傳預設 CONFIG.HEARTBEAT_URL', () => {
    vi.mocked(getLocalStorageItem).mockReturnValue([
      { provider: 'proxy', enabled: true, url: 'https://touka.example.com/api' }
    ]);
    const result = getActiveHeartbeatUrl();
    expect(result).toBe(CONFIG.HEARTBEAT_URL);
  });
});

describe('fetchTouka 404 Fallback — M4 Recursion & Token Sync', () => {
  beforeEach(() => {
    clearAccessToken();
    vi.restoreAllMocks();
  });

  it('當請求非 /v1 開頭且收到 404 時，應遞迴向 /v1 重試，且支援領取 X-New-Auth-Token', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch');

    // 第一次請求 /test 模擬回傳 404
    const res404 = new Response('Not Found', { status: 404 });
    // 第二次請求 /v1/test 模擬回傳 200，且包含領卡 header
    const headers200 = new Headers({
      'X-New-Auth-Token': 'newly-issued-token-xyz',
      'Content-Type': 'application/json'
    });
    const res200 = new Response('{"ok":true}', { status: 200, headers: headers200 });

    fetchSpy.mockResolvedValueOnce(res404).mockResolvedValueOnce(res200);

    const proxyUrl = 'http://localhost:8000';
    const response = await fetchTouka(proxyUrl, '/test');

    // 驗證第一次發向 /test，第二次發向 /v1/test
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy.mock.calls[0][0]).toBe('http://localhost:8000/test');
    expect(fetchSpy.mock.calls[1][0]).toBe('http://localhost:8000/v1/test');

    // 驗證回傳的是第二次成功的回應
    expect(response.status).toBe(200);
    const json = await response.json();
    expect(json.ok).toBe(true);

    // 驗證 token 被正確領取更新至記憶體變數中
    expect(getAccessToken()).toBe('newly-issued-token-xyz');
  });
});
