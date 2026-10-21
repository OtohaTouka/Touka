/**
 * connections — P3 認證縫（四功能區調查 §auth #9）。
 *
 * #9：clearAccessToken 只設 _accessToken=null、不作廢在途 refresh run，晚到的成功
 * 回應會把記憶體 access token 重新填回（登出後半殭屍：UI 已登出、模組仍持有效卡）；
 * 且不清 _knownAgentIds（換帳號時 X-Touka-Soul-ID 解析殘留前帳號 agent）。
 * 修法：token 世代 counter——clearAccessToken 遞增之，在途 run 回填前比對世代。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  refreshAccessToken, clearAccessToken, setAccessToken, getAccessToken,
  setKnownAgentIds, resolveSoulId,
} from '../connections';

describe('connections — P3 認證縫（在途 refresh 復活 + knownAgentIds）', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    clearAccessToken();
  });

  it('#9：登出（clearAccessToken）後在途 refresh 晚到，不得復活記憶體 access token', async () => {
    setAccessToken('old');
    let resolveFetch: (v: unknown) => void = () => {};
    const pending = new Promise(r => { resolveFetch = r; });
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending));

    const p = refreshAccessToken('http://x');   // 啟動慢 refresh
    clearAccessToken();                          // 登出（遞增世代作廢本 run）
    resolveFetch({ ok: true, status: 200, json: async () => ({ access_token: 'new' }) });
    const tok = await p;

    expect(tok).toBeNull();
    expect(getAccessToken()).toBeNull();         // 不得被回填＝無半殭屍
  });

  it('#9：正常 refresh（無登出介入）仍回填 access token', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => ({ access_token: 'fresh' }),
    }));
    const tok = await refreshAccessToken('http://x');
    expect(tok).toBe('fresh');
    expect(getAccessToken()).toBe('fresh');
  });

  it('#9：clearAccessToken 清除 knownAgentIds（換帳號跨帳號殘留關閉）', () => {
    setKnownAgentIds(['Sumi']);
    expect(resolveSoulId('Sumi')).not.toBe('');   // 清除前：解析為已知 agent
    clearAccessToken();
    expect(resolveSoulId('Sumi')).toBe('');        // 清除後：不再解析（殘留關閉）
  });
});
