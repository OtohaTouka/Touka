/**
 * H1 — LOCALSTORAGE_SSOT 修復驗證
 *
 * 驗證 getActiveConnection() 改用 getLocalStorageItem() 後：
 * 1. 無資料時回傳 null
 * 2. 有 proxy 連線時正確回傳
 * 3. 執行過程中 localStorage.getItem 不再被直接呼叫
 * 4. 只回傳 enabled=true 的連線
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

// 模擬 getLocalStorageItem 以便追蹤呼叫
vi.mock('@/hooks/useLocalStorage', () => ({
  getLocalStorageItem: vi.fn(<T>(key: string, fallback: T): T => fallback),
}));

import { getLocalStorageItem } from '@/hooks/useLocalStorage';

describe('getActiveConnection — H1 LOCALSTORAGE_SSOT 修復', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('無 localStorage 資料時應回傳 null', async () => {
    // getLocalStorageItem 回傳空陣列（預設值）
    vi.mocked(getLocalStorageItem).mockReturnValue([]);

    const { getActiveConnection } = await import('@/lib/connections');
    const result = getActiveConnection();
    expect(result).toBeNull();
  });

  it('有 enabled=true 的 proxy 連線時，應正確回傳', async () => {
    const fakeConn = {
      id: 'proxy',
      name: 'ToukaProxy',
      url: 'http://localhost:8000',
      apiKey: '',
      provider: 'proxy',
      protocol: 'openai',
      enabled: true,
    };
    vi.mocked(getLocalStorageItem).mockReturnValue([fakeConn]);

    const { getActiveConnection } = await import('@/lib/connections');
    const result = getActiveConnection();
    expect(result).toEqual(fakeConn);
    expect(result?.url).toBe('http://localhost:8000');
  });

  it('只有 enabled=false 的連線時，應回傳 null', async () => {
    const disabledConn = {
      id: 'proxy',
      name: 'ToukaProxy',
      url: 'http://localhost:8000',
      apiKey: '',
      provider: 'proxy',
      protocol: 'openai',
      enabled: false,
    };
    vi.mocked(getLocalStorageItem).mockReturnValue([disabledConn]);

    const { getActiveConnection } = await import('@/lib/connections');
    const result = getActiveConnection();
    expect(result).toBeNull();
  });

  it('執行時必須呼叫 getLocalStorageItem 而非直接 localStorage.getItem', async () => {
    vi.mocked(getLocalStorageItem).mockReturnValue([]);
    const lsGetSpy = vi.spyOn(Storage.prototype, 'getItem');

    const { getActiveConnection } = await import('@/lib/connections');
    getActiveConnection();

    // getLocalStorageItem 必須被呼叫
    expect(getLocalStorageItem).toHaveBeenCalledWith('touka_connections', []);

    // 直接的 localStorage.getItem 針對 touka_connections 不應被呼叫
    const directCall = lsGetSpy.mock.calls.find(([key]) => key === 'touka_connections');
    expect(directCall).toBeUndefined();

    lsGetSpy.mockRestore();
  });

  it('多個連線時，只回傳第一個 enabled=true 的 proxy 連線', async () => {
    const openAiConn = {
      id: 'openai-1',
      name: 'OpenAI',
      url: 'https://api.openai.com',
      apiKey: 'sk-test',
      provider: 'openai',
      protocol: 'openai',
      enabled: true,
    };
    const proxyConn = {
      id: 'proxy',
      name: 'ToukaProxy',
      url: 'http://localhost:8000',
      apiKey: '',
      provider: 'proxy',
      protocol: 'openai',
      enabled: true,
    };
    vi.mocked(getLocalStorageItem).mockReturnValue([openAiConn, proxyConn]);

    const { getActiveConnection } = await import('@/lib/connections');
    const result = getActiveConnection();
    // 應回傳 proxy，非 openai
    expect(result?.provider).toBe('proxy');
  });
});
