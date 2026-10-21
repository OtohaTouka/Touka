/**
 * iso-c2 U2h：側欄的隔離橫幅（GET /v1/isolation）。
 * 層級低於預期、或埠沒擋（缺 tcp）時出現；自癒額度用完（permission_relock）另一列；denied_burst 不上橫幅。
 */
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
const state = vi.hoisted(() => ({ signedIn: true }));
vi.mock('@/lib/connections', () => ({
  fetchTouka: vi.fn(),
  getActiveProxyUrl: () => 'http://proxy.test',
  captureRequestScope: () => (state.signedIn ? { proxyOrigin: 'http://proxy.test', authEpoch: 0 } : null),
  subscribeCredentialScope: () => () => undefined,
}));
import { fetchTouka } from '@/lib/connections';
import { IsolationBanner } from '../IsolationBanner';
import { ISOLATION_POLL_MS } from '@/hooks/useIsolationStatus';

const ALL = ['abstract_unix', 'fs', 'ioctl', 'signal', 'tcp'];
const LOCAL = { status: 'available', abi: 6, reason: null, net: null };
const REPAIRS = {
  attempted: 0, repaired: 0, refused_outside: 0, refused_protected: 0, refused_not_owner: 0,
  nothing_changed: 0, throttled: 0, retried: 0, retry_failed: 0,
  permission_relock: false, denied_burst: false, window_resets_at: 1000.5,
};
const status = (overrides: Record<string, unknown> = {}) => ({
  level: 'executor', expected: 'executor', reason: null, detail: null, features: ALL, local: LOCAL,
  local_issue: null, executor: { abi: 6, net: null }, block_ports_issue: null, repairs: REPAIRS,
  mcp: { unisolated: 0, waiting: 0, suspended: 0, store_issue: null },
  ...overrides,
});
const reply = (body: unknown, ok = true) => ({ ok, status: ok ? 200 : 500, json: async () => body }) as Response;
const mountWith = async (body: unknown, ok = true) => {
  vi.mocked(fetchTouka).mockResolvedValue(reply(body, ok));
  render(<IsolationBanner />);
  await waitFor(() => expect(fetchTouka).toHaveBeenCalled());
  await act(async () => { await Promise.resolve(); });
};
const banner = () => screen.queryByTestId('isolation-banner');

beforeEach(() => { vi.clearAllMocks(); state.signedIn = true; });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('IsolationBanner', () => {
  it.each([
    [0, 2, 0],
    [0, 0, 3],
    [1, 0, 0],
    [1, 2, 3],
  ])(
    '隔離正常仍顯示 MCP 台數 %s/%s/%s，不洩漏明細',
    async (unisolated, waiting, suspended) => {
      await mountWith(
        status({
          mcp: {
            unisolated,
            waiting,
            suspended,
            store_issue: null,
            name: 'private-server',
            path: '/private/location',
          },
        }),
      );
      const el = await screen.findByTestId('isolation-banner');
      expect(el).toHaveTextContent(
        `MCP：${unisolated} 台未隔離、${waiting} 台等待隔離、${suspended} 台因隔離自測失敗暫停。`,
      );
      expect(el.querySelectorAll('p')).toHaveLength(1);
      expect(el).not.toHaveTextContent(
        /private-server|private\/location|隔離比預期弱/,
      );
    },
  );

  it.each([-1, 0.5, '2', null, undefined])(
    '不顯示無效 MCP 計數 %s',
    async (waiting) => {
      await mountWith(
        status({
          level: 'none',
          features: [],
          reason: 'executor_unreachable',
          mcp: { unisolated: 0, waiting, suspended: 0, store_issue: null },
        }),
      );
      const el = await screen.findByTestId('isolation-banner');
      expect(el).not.toHaveTextContent('MCP：');
      expect(el).toHaveTextContent('隔離比預期弱');
    },
  );

  it('預期沒有隔離時不因未隔離 MCP 顯示橫幅', async () => {
    await mountWith(status({
      level: 'none', expected: 'none', reason: 'native_platform', features: [], executor: null,
      mcp: { unisolated: 3, waiting: 0, suspended: 0, store_issue: null },
    }));
    expect(banner()).toBeNull();
  });

  it('忽略無效計數仍保留其他有效 MCP 計數', async () => {
    await mountWith(status({
      mcp: { unisolated: -1, waiting: 2, suspended: 1, store_issue: null },
    }));
    expect(await screen.findByTestId('isolation-banner')).toHaveTextContent(
      'MCP：0 台未隔離、2 台等待隔離、1 台因隔離自測失敗暫停。',
    );
  });

  it('無效 MCP 計數不隱藏埠未封鎖警告', async () => {
    await mountWith(status({
      features: ['fs'],
      mcp: { unisolated: 0, waiting: 0, suspended: -1, store_issue: null },
    }));
    const el = await screen.findByTestId('isolation-banner');
    expect(el).not.toHaveTextContent('MCP：');
    expect(el).toHaveTextContent('擋不住連往 Touka 內部服務與本機上游埠的連線');
  });

  it('asks the isolation endpoint with the signed-in scope', async () => {
    await mountWith(status());
    expect(fetchTouka).toHaveBeenCalledWith('http://proxy.test', '/v1/isolation', expect.objectContaining({
      cache: 'no-store', requestScope: { proxyOrigin: 'http://proxy.test', authEpoch: 0 },
    }));
  });

  it('stays hidden when isolation is as expected', async () => {
    await mountWith(status());
    expect(banner()).toBeNull();
  });

  it('stays hidden where no isolation is expected (native Windows)', async () => {
    await mountWith(status({ level: 'none', expected: 'none', reason: 'native_platform', features: [], executor: null }));
    expect(banner()).toBeNull();
  });

  it('a down executor shows why, the fix, and the control-channel note', async () => {
    await mountWith(status({ level: 'none', reason: 'executor_unreachable', executor: null, features: [] }));
    const el = await screen.findByTestId('isolation-banner');
    expect(el).toHaveAttribute('role', 'alert');
    expect(el).toHaveTextContent('隔離比預期弱：代理人的指令在 Auto／YOLO 也都要你核准，無人在場時一律拒絕。');
    expect(el).toHaveTextContent(/原因：設定了隔離執行器，但現在連不到它/);
    expect(el).toHaveTextContent(/執行器控制通道的權限[\s\S]*每 5 秒會自己修回/);
  });

  it('an untrusted interpreter on a landlock deployment shows the reason and the rebuild', async () => {
    await mountWith(status({
      level: 'none', expected: 'landlock', reason: 'interpreter_untrusted', executor: null, features: [],
      local: { status: 'unavailable', abi: 0, reason: 'interpreter_untrusted', net: null },
      local_issue: 'interpreter_untrusted',
    }));
    const el = await screen.findByTestId('isolation-banner');
    expect(el).toHaveTextContent(/原因：Touka 用來啟動隔離的 Python 不可信[\s\S]*docker compose up -d --build/);
    expect(el).not.toHaveTextContent(/控制通道/);
  });

  it('an unreadable connections.json names the file and the recovery', async () => {
    await mountWith(status({
      level: 'none', reason: 'block_ports_incomplete', block_ports_issue: 'block_ports_incomplete',
      features: ['fs', 'signal'],
    }));
    const el = await screen.findByTestId('isolation-banner');
    expect(el).toHaveTextContent(/connections\.json[\s\S]*約半分鐘內會自動恢復/);
    expect(el).not.toHaveTextContent(/控制通道/);
  });

  it('an unsafe network on the executor names the override for both services', async () => {
    await mountWith(status({ level: 'none', reason: 'net_mptcp', executor: { abi: 6, net: 'net_mptcp' }, features: ['fs'] }));
    expect(await screen.findByTestId('isolation-banner')).toHaveTextContent(/touka-proxy 與 touka-executor[\s\S]*net\.mptcp\.enabled: 0/);
  });

  it('an executor without tcp rules (ABI 3) says the ports are not blocked', async () => {
    await mountWith(status({ executor: { abi: 3, net: null }, features: ['fs'] }));
    expect(await screen.findByTestId('isolation-banner')).toHaveTextContent(/擋不住連往 Touka 內部服務與本機上游埠的連線/);
  });

  it('a used-up repair budget gets its own line even when isolation is fine', async () => {
    await mountWith(status({ repairs: { ...REPAIRS, permission_relock: true } }));
    const el = await screen.findByTestId('isolation-banner');
    expect(el).toHaveTextContent('有代理人反覆鎖住自己的工作區權限');
    expect(el).not.toHaveTextContent('隔離比預期弱');
  });

  it('a burst of refused repairs alone is not a banner', async () => {
    await mountWith(status({ repairs: { ...REPAIRS, denied_burst: true } }));
    expect(banner()).toBeNull();
  });

  it.each([
    ['an HTTP error', status({ level: 'none', reason: 'executor_unreachable' }), false],
    ['a malformed body', { level: 'none', expected: 'executor' }, true],
    ['an unknown level', status({ level: 'sandboxed', repairs: { ...REPAIRS, permission_relock: true } }), true],
  ])('shows nothing for %s', async (_name, body, ok) => {
    await mountWith(body, ok);
    expect(banner()).toBeNull();
  });

  it('does not ask before sign-in', async () => {
    state.signedIn = false;
    render(<IsolationBanner />);
    await act(async () => { await new Promise(done => setTimeout(done, 20)); });
    expect(fetchTouka).not.toHaveBeenCalled();
    expect(banner()).toBeNull();
  });

  it.each([undefined, null, [], 'invalid', 0])('ignores a missing or malformed mcp summary: %s', async mcp => {
    await mountWith(status({ level: 'none', reason: 'executor_unreachable', mcp }));
    expect(banner()).toBeNull();
  });

  it('checks again on its own so a recovered executor clears the banner', async () => {
    vi.useFakeTimers();
    vi.mocked(fetchTouka).mockResolvedValue(reply(status({ level: 'none', reason: 'executor_restarted', features: [] })));
    render(<IsolationBanner />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(banner()).not.toBeNull();
    vi.mocked(fetchTouka).mockResolvedValue(reply(status()));
    await act(async () => { await vi.advanceTimersByTimeAsync(ISOLATION_POLL_MS); });
    expect(banner()).toBeNull();
    expect(fetchTouka).toHaveBeenCalledTimes(2);
  });
});
