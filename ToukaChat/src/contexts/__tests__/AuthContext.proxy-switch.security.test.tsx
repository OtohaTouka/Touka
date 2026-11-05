/**
 * 換 Proxy 網址時，登入憑證與連線清單的去向。
 * 真的 AuthProvider、SystemStatusProvider、AuthGuard、設定頁與 connections.ts；只有 fetch 和提示框是假的，
 * 假 fetch 記下每個請求的順序、網址、方法、憑證與內容，斷言一律看實際送出的請求。
 */
import React, { useEffect } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const flags = vi.hoisted(() => ({ plainSelect: false, plainSwitch: false }));
const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
const toasts = vi.hoisted(() => ({
  warning: vi.fn(), success: vi.fn(), error: vi.fn(), dismiss: vi.fn(), loading: vi.fn(() => 'toast-id'),
}));

vi.mock('next/navigation', () => ({ useRouter: () => router, usePathname: () => '/' }));
vi.mock('sonner', () => ({ toast: toasts }));
vi.mock('@/components/LeftSidebar', () => ({ default: () => null }));
// 表單裡的第三方下拉內部的寫法，碰巧讓「事件早於寫入」不會發生；換成普通元件才走得到那個順序。
vi.mock('@/components/ui/select', async importOriginal => {
  const real = await importOriginal<typeof import('@/components/ui/select')>();
  type Props = { children?: React.ReactNode };
  const plain = ({ children }: Props) => <div>{children}</div>;
  const swap = <P extends Props>(Real: React.ComponentType<P>, Plain: React.ComponentType<Props>) =>
    function Swapped(props: P) { return flags.plainSelect ? <Plain>{props.children}</Plain> : <Real {...props} />; };
  return {
    ...real,
    Select: swap(real.Select as unknown as React.ComponentType<Props>, plain),
    SelectContent: swap(real.SelectContent as unknown as React.ComponentType<Props>, () => null),
    SelectItem: swap(real.SelectItem as unknown as React.ComponentType<Props>, () => null),
    SelectTrigger: swap(real.SelectTrigger as unknown as React.ComponentType<Props>, plain),
    SelectValue: swap(real.SelectValue as unknown as React.ComponentType<Props>, () => null),
  };
});
vi.mock('@/components/ui/switch', async importOriginal => {
  const real = await importOriginal<typeof import('@/components/ui/switch')>();
  type Props = { checked?: boolean; onCheckedChange?: (next: boolean) => void };
  return {
    ...real,
    Switch: (props: Props) => flags.plainSwitch
      ? <button type="button" role="switch" aria-checked={props.checked} onClick={() => props.onCheckedChange?.(!props.checked)} />
      : <real.Switch {...props} />,
  };
});

import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { AuthGuard } from '@/components/AuthGuard';
import { SystemStatusProvider } from '@/lib/SystemStatusContext';
import { ConnectionsTab } from '@/components/settings/tabs/ConnectionsTab';
import ConnectionsPage from '@/app/connections/page';
import { useConnectionSettings } from '@/hooks/settings/useConnectionSettings';
import { clearAccessToken, fetchTouka, getAccessToken, getActiveProxyUrl } from '@/lib/connections';
import type { Connection } from '@/lib/types';

const OLD = 'http://old.test:8000';
const NEW = 'http://new.test:9000';
const THIRD = 'http://third.test:7000';
const TEMPLATE_PROXY = 'http://localhost:8000';
const LOCAL_KEY = 'sk-local-unmasked';

const proxy = (url: string, id = 'proxy', enabled = true) =>
  ({ id, name: id, url, provider: 'proxy', protocol: 'openai', enabled, apiKey: '' } as Connection);
const custom = (id: string, apiKey = '') =>
  ({ id, name: id, url: `http://${id}.test`, provider: 'custom', protocol: 'openai', enabled: true, apiKey } as Connection);
const b64 = (value: object) => btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
const card = (name: string, sub = 'u1') => `${b64({ alg: 'none' })}.${b64({ type: 'access', sub, exp: 4_000_000_000 })}.${name}`;
const OLD_CARD = card('oldcard');
const NEW_CARD = card('newcard', 'u2');
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const HEALTH = { status: 'healthy', timestamp: '2026-11-05T00:00:00Z', permission_config_degraded: false, permission_config_degraded_reason: '' };

interface Call { url: string; method: string; auth: string | null; credentials?: RequestCredentials; redirect?: RequestRedirect; body: string | null }
type Health = 'ok' | 'down' | 'html' | Promise<'ok' | 'down'>;

let calls: Call[] = [];
let oldList: Connection[] = [];
let newList: Connection[] = [];
let newHealth: Health = 'ok';
let oldListResponse: Promise<void> | null = null;

const profile = (id: string) => ({ id, email: 'a@b.c', role: 'admin', status: 'active', owned_agents: [] });

/** 目前登入中的後端：有 refresh cookie 的那台，空字串是前端來源。 */
function loggedInBackend(url: string, init: RequestInit | undefined, list: () => Connection[], setList: (next: Connection[]) => void) {
  const method = init?.method ?? 'GET';
  if (url.endsWith('/auth/refresh')) return json({ access_token: OLD_CARD, token_type: 'bearer' });
  if (url.endsWith('/auth/me')) return json(profile('u1'));
  if (url.endsWith('/v1/health')) return json(HEALTH);
  if (url.endsWith('/v1/memory')) return json({ system_degraded: false, degraded_reason: '', memory_tools_count: 1 });
  if (url.endsWith('/v1/ui/connections') && method === 'POST') { setList(JSON.parse(String(init?.body))); return json({ status: 'success' }); }
  if (url.endsWith('/v1/ui/connections')) return json(list());
  if (url.endsWith('/v1/ui/connections/status')) return json({});
  return json({ status: 'success' });
}

async function otherBackend(url: string, init?: RequestInit) {
  const method = init?.method ?? 'GET';
  if (url.endsWith('/v1/health')) {
    const state = await newHealth;
    if (state === 'down') throw new TypeError('Failed to fetch');
    if (state === 'html') return new Response('<html>not touka</html>', { status: 200, headers: { 'Content-Type': 'text/html' } });
    return json(HEALTH);
  }
  if (url.endsWith('/auth/refresh')) return json({ detail: 'no cookie' }, 401);
  if (url.endsWith('/auth/login')) return json({ access_token: NEW_CARD, token_type: 'bearer' });
  if (url.endsWith('/auth/me')) return json(profile('u2'));
  if (url.endsWith('/v1/ui/connections') && method === 'POST') { newList = JSON.parse(String(init?.body)); return json({ status: 'success' }); }
  if (url.endsWith('/v1/ui/connections')) return json(newList);
  if (url.endsWith('/v1/ui/connections/status')) return json({});
  return json({ status: 'success' });
}

const authApi: { current: ReturnType<typeof useAuth> | null } = { current: null };
function ExposeAuth() {
  const auth = useAuth();
  useEffect(() => { authApi.current = auth; });
  return null;
}

const settingsApi: { current: ReturnType<typeof useConnectionSettings> | null } = { current: null };
function ExposeSettings({ fetchOnMount = false }: { fetchOnMount?: boolean }) {
  const hook = useConnectionSettings();
  useEffect(() => { settingsApi.current = hook; });
  const { fetchConnections } = hook;
  useEffect(() => { if (fetchOnMount) void fetchConnections(); }, [fetchOnMount, fetchConnections]);
  return <div data-testid="settings" />;
}

const tree = (children: React.ReactNode) => (
  <AuthProvider>
    <SystemStatusProvider>
      <ExposeAuth />
      {children}
    </SystemStatusProvider>
  </AuthProvider>
);
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 60)); }); };
const signedIn = () => authApi.current?.isAuthenticated === true;

async function renderTab() {
  render(tree(<AuthGuard><ConnectionsTab isSystemRefreshing={false} onRefresh={() => {}} /></AuthGuard>));
  await waitFor(() => expect(signedIn()).toBe(true));
  await screen.findAllByRole('heading');
  await settle();
  calls = [];
}
/** 不包 AuthGuard：登出後 hook 還掛著，才看得出丟棄回應的判斷有沒有作用。 */
async function renderSettings({ guard = true, fetchOnMount = false } = {}) {
  const inner = <ExposeSettings fetchOnMount={fetchOnMount} />;
  render(tree(guard ? <AuthGuard>{inner}</AuthGuard> : inner));
  await waitFor(() => expect(signedIn()).toBe(true));
  await settle();
}

const buttonNamed = (pattern: RegExp) => screen.getAllByRole('button').find(button =>
  pattern.test(button.getAttribute('aria-label') ?? '') || pattern.test(button.textContent ?? ''))!;
function editUrl(name: string, from: string, to: string) {
  fireEvent.click(buttonNamed(new RegExp(`編輯連線 ${name}`)));
  fireEvent.change(screen.getAllByRole('textbox').find(input => (input as HTMLInputElement).value === from)!, { target: { value: to } });
  fireEvent.click(buttonNamed(/儲存/));
}

type ToastOptions = { action?: { onClick: () => void }; cancel?: { onClick: () => void } };
const lastPrompt = (): ToastOptions | undefined => toasts.warning.mock.calls.at(-1)?.[1] as ToastOptions | undefined;
/** 有確認提示才按：修前沒有提示就照走完流程，讓紅燈落在憑證與清單上。 */
async function confirmIfAsked() {
  await act(async () => { lastPrompt()?.action?.onClick(); });
  await settle();
}
async function cancelIfAsked() {
  await act(async () => { lastPrompt()?.cancel?.onClick(); });
  await settle();
}
async function otherTabSwitchesTo(url: string, notify = true) {
  await act(async () => {
    window.localStorage.setItem('touka_connections', JSON.stringify([proxy(url)]));
    if (notify) window.dispatchEvent(new StorageEvent('storage', { key: 'touka_connections' }));
  });
}

const callsTo = (origin: string) => calls.filter(call => call.url.startsWith(origin));
const cardsSentTo = (origin: string) => callsTo(origin).filter(call => call.auth !== null);
const listsSentTo = (origin: string) => callsTo(origin).filter(call => call.url.endsWith('/v1/ui/connections') && call.method === 'POST');
const logoutsAt = (origin: string) => callsTo(origin).filter(call => call.url.endsWith('/v1/auth/logout'));
const storedList = (): Connection[] => JSON.parse(window.localStorage.getItem('touka_connections') ?? '[]');

function expectStillOnOld() {
  expect(getActiveProxyUrl()).toBe(OLD);
  expect(getAccessToken()).toBe(OLD_CARD);
  expect(signedIn()).toBe(true);
}
function expectSwitched(to: string, from = OLD) {
  expect(getActiveProxyUrl()).toBe(to);
  expect(getAccessToken()).toBeNull();
  expect(signedIn()).toBe(false);
  expect(cardsSentTo(to)).toEqual([]);
  expect(listsSentTo(to)).toEqual([]);
  expect(logoutsAt(from)).toHaveLength(1);
}

beforeEach(() => {
  calls = [];
  newHealth = 'ok';
  oldListResponse = null;
  flags.plainSelect = false;
  flags.plainSwitch = false;
  newList = [];
  oldList = [proxy(OLD)];
  vi.clearAllMocks();
  toasts.loading.mockReturnValue('toast-id');
  clearAccessToken();
  window.localStorage.clear();
  window.localStorage.setItem('touka_connections', JSON.stringify(oldList));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({
      url, method: init?.method ?? 'GET', auth: new Headers(init?.headers).get('Authorization'),
      credentials: init?.credentials, redirect: init?.redirect, body: typeof init?.body === 'string' ? init.body : null,
    });
    if (url.startsWith('http') && !url.startsWith(OLD)) return otherBackend(url, init);
    if (url.endsWith('/v1/ui/connections') && (init?.method ?? 'GET') === 'GET' && oldListResponse) await oldListResponse;
    return loggedInBackend(url, init, () => oldList, next => { oldList = next; });
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clearAccessToken();
});

describe('在設定頁換網址', () => {
  it('T1 確認後：先向舊後端登出，新網址收不到憑證也收不到清單', async () => {
    oldList = [proxy(OLD), custom('c3', LOCAL_KEY)];
    window.localStorage.setItem('touka_connections', JSON.stringify(oldList));
    await renderTab();

    editUrl('proxy', OLD, NEW);
    await settle();
    await confirmIfAsked();

    expectSwitched(NEW);
    const body = callsTo(NEW).map(call => call.body ?? '').join('');
    expect(body).not.toContain(LOCAL_KEY);
    const probe = callsTo(NEW).find(call => call.url.endsWith('/v1/health'))!;
    expect(probe.credentials).toBe('omit');
    expect(probe.redirect).toBe('error');
    const logout = logoutsAt(OLD)[0];
    expect(logout.credentials).toBe('include');
    expect(logout.auth).toBe(`Bearer ${OLD_CARD}`);
    const firstWithCookie = calls.findIndex(call => call.url.startsWith(NEW) && call.credentials === 'include');
    expect(firstWithCookie).toBeGreaterThan(calls.indexOf(logout));
    expect(router.push).toHaveBeenCalledWith('/login');
  });

  it('T1b 取消：什麼都沒變，也沒有請求送到新網址', async () => {
    await renderTab();

    editUrl('proxy', OLD, NEW);
    await settle();
    await cancelIfAsked();

    expect(toasts.warning).toHaveBeenCalled();
    expectStillOnOld();
    expect(callsTo(NEW)).toEqual([]);
    expect(logoutsAt(OLD)).toEqual([]);
    expect(storedList()[0].url).toBe(OLD);
  });

  it('T1c 確認提示寫明新網址、影響所有裝置與回登入頁', async () => {
    await renderTab();

    editUrl('proxy', OLD, NEW);
    await settle();

    const message = String(toasts.warning.mock.calls.at(-1)?.[0]);
    expect(message).toContain(NEW);
    expect(message).toContain('所有裝置');
    expect(message).toContain('登入頁');
  });

  it('T2 表單下拉換成普通元件時，勾選切換也一樣', async () => {
    flags.plainSelect = true;
    oldList = [proxy(NEW, 'pnew', false), proxy(OLD, 'pold')];
    window.localStorage.setItem('touka_connections', JSON.stringify(oldList));
    await renderTab();

    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    await settle();
    await confirmIfAsked();
    await act(async () => { await fetchTouka(getActiveProxyUrl(), '/v1/ui/sessions'); });

    expectSwitched(NEW);
  });

  it.each(['down', 'html'] as const)('T3 新網址 %s：不換網址、仍登入、可以重試', async state => {
    newHealth = state;
    await renderTab();

    editUrl('proxy', OLD, NEW);
    await settle();
    await confirmIfAsked();

    expectStillOnOld();
    expect(logoutsAt(OLD)).toEqual([]);
    expect(listsSentTo(NEW)).toEqual([]);
    expect(cardsSentTo(NEW)).toEqual([]);
    expect(storedList()[0].url).toBe(OLD);
    expect(toasts.error).toHaveBeenCalledWith(expect.stringContaining(NEW), { id: 'toast-id' });

    newHealth = 'ok';
    editUrl('proxy', OLD, NEW);
    await settle();
    await confirmIfAsked();
    expectSwitched(NEW);
  });

  it('T3c 探測進行中再按一次確認：只切一次', async () => {
    let finishProbe!: (state: 'ok') => void;
    newHealth = new Promise(resolve => { finishProbe = resolve; });
    await renderTab();

    editUrl('proxy', OLD, NEW);
    await settle();
    await act(async () => { lastPrompt()?.action?.onClick(); });
    await act(async () => { lastPrompt()?.action?.onClick(); });
    await waitFor(() => expect(callsTo(NEW).filter(call => call.url.endsWith('/v1/health'))).toHaveLength(1));
    await act(async () => { finishProbe('ok'); });
    await settle();

    expect(callsTo(NEW).filter(call => call.credentials === 'omit')).toHaveLength(1);
    expectSwitched(NEW);
  });

  it('T3d 第一個確認完成後，再按留下來的第二個提示：什麼都不再發生', async () => {
    await renderSettings({ guard: false });
    await act(async () => { await settingsApi.current!.saveConnections([proxy(NEW)]); });
    const first = lastPrompt();
    await act(async () => { await settingsApi.current!.saveConnections([proxy(THIRD)]); });
    const second = lastPrompt();
    calls = [];

    await act(async () => { first?.action?.onClick(); });
    await settle();
    expectSwitched(NEW);
    const before = calls.length;
    await act(async () => { second?.action?.onClick(); });
    await settle();

    expect(getActiveProxyUrl()).toBe(NEW);
    expect(logoutsAt(OLD)).toHaveLength(1);
    expect(logoutsAt(NEW)).toEqual([]);
    expect(callsTo(THIRD)).toEqual([]);
    expect(calls.slice(before).filter(call => call.url.endsWith('/logout'))).toEqual([]);
  });

  it('T3e 探測進行中別的分頁換了網址：不多打登出、不蓋掉別的分頁的選擇', async () => {
    let finishProbe!: (state: 'ok') => void;
    newHealth = new Promise(resolve => { finishProbe = resolve; });
    await renderSettings({ guard: false });
    await act(async () => { await settingsApi.current!.saveConnections([proxy(NEW)]); });
    calls = [];

    await act(async () => { lastPrompt()?.action?.onClick(); });
    await waitFor(() => expect(callsTo(NEW)).toHaveLength(1));
    await otherTabSwitchesTo(THIRD);
    await act(async () => { finishProbe('ok'); });
    await settle();

    expect(getActiveProxyUrl()).toBe(THIRD);
    expect(storedList()[0].url).toBe(THIRD);
    expect(logoutsAt(OLD)).toHaveLength(1);
    expect(listsSentTo(THIRD)).toEqual([]);
    expect(getAccessToken()).toBeNull();
    expect(toasts.dismiss).toHaveBeenCalledWith('toast-id');
  });

  it('T3e2 向舊後端登出時別的分頁換了網址：不蓋掉別的分頁的選擇', async () => {
    await renderSettings({ guard: false });
    await act(async () => { await settingsApi.current!.saveConnections([proxy(NEW)]); });
    calls = [];
    const fetchMock = vi.mocked(globalThis.fetch);
    const answer = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/v1/auth/logout')) await otherTabSwitchesTo(THIRD, false);
      return answer(input, init);
    });

    await act(async () => { lastPrompt()?.action?.onClick(); });
    await settle();
    expect(storedList()[0].url).toBe(THIRD);

    await act(async () => { window.dispatchEvent(new StorageEvent('storage', { key: 'touka_connections' })); });
    await settle();

    expect(getActiveProxyUrl()).toBe(THIRD);
    expect(storedList()[0].url).toBe(THIRD);
    expect(listsSentTo(THIRD)).toEqual([]);
    expect(cardsSentTo(THIRD)).toEqual([]);
    expect(getAccessToken()).toBeNull();
  });

  it('T3f 切換進行中存另一筆：直接拒絕，不送任何清單', async () => {
    let finishProbe!: (state: 'ok') => void;
    newHealth = new Promise(resolve => { finishProbe = resolve; });
    await renderSettings({ guard: false });
    await act(async () => { await settingsApi.current!.saveConnections([proxy(NEW)]); });
    await act(async () => { lastPrompt()?.action?.onClick(); });
    await waitFor(() => expect(callsTo(NEW).length).toBeGreaterThan(0));
    calls = [];

    let saved: boolean | undefined;
    await act(async () => { saved = await settingsApi.current!.saveConnections([proxy(OLD), custom('c9')]); });

    expect(saved).toBe(false);
    expect(listsSentTo(OLD)).toEqual([]);
    expect(listsSentTo(NEW)).toEqual([]);
    await act(async () => { finishProbe('ok'); });
    await settle();
    expect(storedList().map(conn => conn.id)).toEqual(['proxy']);
  });

  it('T8 同網址只改其他欄位：不登出，存到同一台後端並帶憑證', async () => {
    oldList = [proxy(OLD), custom('c3')];
    window.localStorage.setItem('touka_connections', JSON.stringify(oldList));
    await renderTab();

    fireEvent.click(screen.getAllByRole('checkbox')[1]);
    await settle();

    expect(toasts.warning).not.toHaveBeenCalled();
    expectStillOnOld();
    const saved = listsSentTo(OLD);
    expect(saved).toHaveLength(1);
    expect(saved[0].auth).toBe(`Bearer ${OLD_CARD}`);
    expect(callsTo(NEW)).toEqual([]);
  });

  it('T8b 同一台後端換個寫法（尾斜線、/v1）不算換網址', async () => {
    await renderTab();

    editUrl('proxy', OLD, `${OLD}/v1/`);
    await settle();

    expect(toasts.warning).not.toHaveBeenCalled();
    expectStillOnOld();
    expect(listsSentTo(OLD)).toHaveLength(1);
  });

  it('T9 切換後在新網址登入：卡綁新網址，舊後端的清單項目被新後端的換掉', async () => {
    oldList = [proxy(NEW, 'pnew', false), proxy(OLD, 'pold'), custom('c-old', LOCAL_KEY)];
    window.localStorage.setItem('touka_connections', JSON.stringify(oldList));
    newList = [custom('c-new')];
    await renderTab();

    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    await settle();
    await confirmIfAsked();
    expectSwitched(NEW);

    await act(async () => { await authApi.current!.login('a@b.c', 'pw'); });
    await settle();

    expect(getAccessToken()).toBe(NEW_CARD);
    expect(callsTo(NEW).find(call => call.url.endsWith('/auth/me'))?.auth).toBe(`Bearer ${NEW_CARD}`);
    expect(callsTo(OLD).filter(call => call.auth === `Bearer ${NEW_CARD}`)).toEqual([]);
    expect(storedList().map(conn => conn.id)).toEqual(['pnew', 'pold', 'c-new']);

    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[checkboxes.length - 1]);
    await settle();

    const saved = listsSentTo(NEW).at(-1)!;
    expect(saved.auth).toBe(`Bearer ${NEW_CARD}`);
    expect(saved.body).not.toContain('c-old');
    expect(saved.body).not.toContain(LOCAL_KEY);
    // 舊 Proxy 項目本身還留在清單裡；這裡只有它的網址，沒有其他連線的金鑰。
    expect(saved.body).toContain('pold');
  });

  it('T10 停用目前的 Proxy、回到預設網址（空字串）：走同一個切換流程', async () => {
    await renderTab();

    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    await settle();
    expect(String(toasts.warning.mock.calls.at(-1)?.[0])).toContain(window.location.origin);
    await confirmIfAsked();

    expect(getActiveProxyUrl()).toBe('');
    expect(getAccessToken()).toBeNull();
    expect(logoutsAt(OLD)).toHaveLength(1);
    const relative = calls.filter(call => call.url.startsWith('/'));
    expect(relative.find(call => call.url === '/v1/health')?.credentials).toBe('omit');
    expect(relative.filter(call => call.url.endsWith('/v1/ui/connections'))).toEqual([]);
    expect(relative.filter(call => call.auth !== null)).toEqual([]);
  });

  it('T10b 刪掉目前的 Proxy、換成下一個：走同一個切換流程', async () => {
    oldList = [proxy(OLD, 'pold'), proxy(NEW, 'pnew')];
    window.localStorage.setItem('touka_connections', JSON.stringify(oldList));
    await renderTab();

    fireEvent.click(buttonNamed(/移除連線 pold/));
    await confirmIfAsked(); // 刪除的確認
    await confirmIfAsked(); // 換網址的確認

    expectSwitched(NEW);
  });
});

describe('後端存的清單不能決定這個瀏覽器連哪裡', () => {
  it('T4 打開設定頁，後端清單指向別的網址：網址不變、仍登入，其他連線照常套用', async () => {
    oldList = [proxy(NEW), custom('c-server')];
    await renderTab();
    await settle();

    expectStillOnOld();
    expect(callsTo(NEW)).toEqual([]);
    expect(storedList().map(conn => conn.id)).toEqual(['proxy', 'c-server']);
    expect(storedList()[0].url).toBe(OLD);
    await act(async () => { await fetchTouka(getActiveProxyUrl(), '/v1/ui/sessions'); });
    expect(calls.at(-1)?.url).toBe(`${OLD}/v1/ui/sessions`);
    expect(calls.at(-1)?.auth).toBe(`Bearer ${OLD_CARD}`);
  });

  it('T4b 全新瀏覽器、後端是預設範本：不會被帶到範本的網址', async () => {
    window.localStorage.removeItem('touka_connections');
    oldList = [proxy(TEMPLATE_PROXY), custom('c-server')];
    render(tree(<AuthGuard><ConnectionsTab isSystemRefreshing={false} onRefresh={() => {}} /></AuthGuard>));
    await waitFor(() => expect(signedIn()).toBe(true));
    await settle();

    expect(getActiveProxyUrl()).toBe('');
    expect(signedIn()).toBe(true);
    expect(getAccessToken()).toBe(OLD_CARD);
    expect(callsTo(TEMPLATE_PROXY)).toEqual([]);
    expect(storedList().map(conn => conn.id)).toEqual(['c-server']);
  });

  it('T4c 清單回應到達前網址已換：這份回應不算數', async () => {
    let answer!: () => void;
    oldList = [custom('c-late')];
    await renderSettings({ guard: false });
    oldListResponse = new Promise(resolve => { answer = resolve; });
    const pending = settingsApi.current!.fetchConnections();
    await waitFor(() => expect(callsTo(OLD).some(call => call.url.endsWith('/v1/ui/connections'))).toBe(true));

    await otherTabSwitchesTo(NEW);
    await act(async () => { answer(); await pending; });
    await settle();

    expect(storedList().map(conn => conn.url)).toEqual([NEW]);
  });
});

describe('別的分頁換了網址', () => {
  it('T5 收到通知：馬上登出，舊後端收到登出，新網址收不到舊憑證', async () => {
    await renderTab();

    await otherTabSwitchesTo(NEW);
    await settle();
    await act(async () => { await fetchTouka(getActiveProxyUrl(), '/v1/ui/sessions'); });

    expectSwitched(NEW);
    expect(logoutsAt(OLD)[0].auth).toBe(`Bearer ${OLD_CARD}`);
  });

  it('T5b 整個 storage 被清掉（key 是 null）也一樣', async () => {
    await renderTab();

    await act(async () => {
      window.localStorage.clear();
      window.dispatchEvent(new StorageEvent('storage', { key: null }));
    });
    await settle();

    expect(getActiveProxyUrl()).toBe('');
    expect(getAccessToken()).toBeNull();
    expect(logoutsAt(OLD)).toHaveLength(1);
  });

  it('T5c 別的設定值變更：不影響登入', async () => {
    await renderTab();

    await act(async () => { window.dispatchEvent(new StorageEvent('storage', { key: 'touka_theme' })); });
    await settle();

    expectStillOnOld();
    expect(logoutsAt(OLD)).toEqual([]);
  });

  it('T6 通知還沒到：下一個請求到新網址也不帶舊憑證', async () => {
    await renderTab();

    await otherTabSwitchesTo(NEW, false);
    await act(async () => { await fetchTouka(getActiveProxyUrl(), '/v1/ui/sessions'); });

    expect(calls.at(-1)?.url).toBe(`${NEW}/v1/ui/sessions`);
    expect(cardsSentTo(NEW)).toEqual([]);
  });
});

describe('舊的 /connections 頁', () => {
  it('T7 第二次操作才換網址：事件發出時讀到的已經是新網址', async () => {
    flags.plainSwitch = true;
    oldList = [proxy(OLD, 'p1'), proxy(NEW, 'p2', false)];
    window.localStorage.setItem('touka_connections', JSON.stringify(oldList));
    const seen: string[] = [];
    const record = () => seen.push(getActiveProxyUrl());
    window.addEventListener('touka_connections_updated', record);
    render(tree(<AuthGuard><ConnectionsPage /></AuthGuard>));
    await waitFor(() => expect(signedIn()).toBe(true));
    await screen.findAllByRole('switch');
    await settle();

    await act(async () => { fireEvent.click(screen.getAllByRole('switch')[1]); });
    await settle();
    await act(async () => { fireEvent.click(screen.getAllByRole('switch')[0]); });
    await settle();
    window.removeEventListener('touka_connections_updated', record);

    expect(seen).toEqual([OLD, NEW]);
    expect(getAccessToken()).toBeNull();
    expect(logoutsAt(OLD)).toHaveLength(1);
    expect(cardsSentTo(NEW)).toEqual([]);
  });
});
