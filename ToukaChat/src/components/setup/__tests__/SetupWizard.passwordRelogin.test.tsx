/**
 * 首次設定精靈：第 2 步改完管理員密碼後請使用者重新登入，登入後從下一步接續（ADR-0053 補記 2026-10-23）。
 *
 * 後端改密碼會撤銷這個人所有舊卡（access 與 refresh cookie；ADR-0144，不放寬）。修正前精靈直接
 * 前進到第 3 步，下一個請求就 401、續期也 401，使用者在精靈中途莫名被帶回登入頁。這裡用真的
 * connections（送卡、401 續期、session-expired 通知）配一個照後端規則運作的假伺服器，
 * 從側欄的提供者打開精靈走一遍。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { SetupNoticesProvider, useSetupNotices } from '../SetupNoticesProvider';
import type { SetupStatus } from '@/hooks/useSetupStatus';
import {
  clearAccessToken, setAccessToken, setSessionExpiredHandler,
} from '@/lib/connections';
import { getSystemEpoch } from '@/lib/time_utils';

const ADMIN = { id: 'admin-1', email: 'admin@example.com', role: 'admin' };
const logout = vi.fn(() => Promise.resolve());
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: ADMIN, logout }) }));

const NEW_PASSWORD = 'Strong#Pass1';
const RESUME_KEY = 'touka_setup_resume';

const status: SetupStatus = {
  dispatch_token_is_default: true,
  auth_secret_persisted: false,
  api_keys_configured: [],
  is_admin_temp_password: true,
  data_path_is_default: true,
  workspace_root_is_default: true,
  embedding_model: 'all-MiniLM-L6-v2',
};

/** 照後端規則運作的假伺服器：改密碼即撤銷所有舊卡與 refresh cookie。 */
function makeBackend() {
  let refreshAlive = true;
  let seq = 0;
  let holdPatch: Promise<void> | null = null;
  const live = new Set<string>();
  const b64url = (o: object) =>
    btoa(JSON.stringify(o)).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
  const issue = (): string => {
    seq += 1;
    const now = Math.floor(getSystemEpoch() / 1000);
    const token = `h.${b64url({ sub: ADMIN.id, type: 'access', iat: now, exp: now + 3600, n: seq })}.s`;
    live.add(token);
    return token;
  };
  const json = (code: number, body: unknown) =>
    new Response(JSON.stringify(body), { status: code, headers: { 'Content-Type': 'application/json' } });
  const handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    // 測試環境沒設 proxy 網址，請求是相對路徑。
    const { pathname } = new URL(String(input), 'http://touka.test');
    const method = init?.method ?? 'GET';
    const bearer = new Headers(init?.headers).get('Authorization')?.replace(/^Bearer /, '') ?? '';
    if (pathname === '/v1/auth/refresh' && method === 'POST') {
      return refreshAlive ? json(200, { access_token: issue(), token_type: 'bearer' }) : json(401, { detail: 'dead' });
    }
    if (!live.has(bearer)) return json(401, { detail: 'stale' });
    if (pathname === '/v1/auth/me') return json(200, ADMIN);
    if (pathname === '/v1/admin/setup/status') return json(200, status);
    if (pathname === `/v1/admin/users/${ADMIN.id}` && method === 'PATCH') {
      const body = JSON.parse(String(init?.body));
      if (body.password === 'Rejected#Pass1') return json(422, { detail: '密碼不合規定' });
      live.clear();
      refreshAlive = false;
      if (holdPatch) await holdPatch;
      return json(200, ADMIN);
    }
    return json(404, {});
  };
  return {
    issue,
    fetch: vi.fn(handle),
    /** 讓改密碼的回應晚到，直到回傳的函式被呼叫。 */
    delayPatch(): () => void {
      let release = () => {};
      holdPatch = new Promise((resolve) => { release = resolve; });
      return release;
    },
  };
}

function OpenAdminPasswordStep() {
  const { openWizard } = useSetupNotices();
  return <button type="button" onClick={() => openWizard('admin_password')}>open</button>;
}

function renderWizard() {
  const view = render(
    <SetupNoticesProvider>
      <OpenAdminPasswordStep />
    </SetupNoticesProvider>,
  );
  fireEvent.click(screen.getByRole('button', { name: 'open' }));
  return view;
}

function submitNewPassword(password = NEW_PASSWORD) {
  fireEvent.change(screen.getByLabelText('新密碼'), { target: { value: password } });
  fireEvent.change(screen.getByLabelText('確認密碼'), { target: { value: password } });
  fireEvent.click(screen.getByRole('button', { name: /設定密碼/ }));
}

const storedResume = () => {
  const raw = window.localStorage.getItem(RESUME_KEY);
  return raw === null ? null : JSON.parse(raw);
};

describe('精靈改完密碼：請重新登入，登入後從下一步接續', () => {
  let backend: ReturnType<typeof makeBackend>;
  const onExpired = vi.fn();

  beforeEach(() => {
    window.localStorage.clear();
    vi.clearAllMocks();
    backend = makeBackend();
    vi.stubGlobal('fetch', backend.fetch);
    setAccessToken(backend.issue());
    setSessionExpiredHandler(onExpired);
  });
  afterEach(() => {
    setSessionExpiredHandler(null);
    clearAccessToken();
    vi.unstubAllGlobals();
  });

  it('改完密碼：顯示重新登入說明、記下接續第 3 步，沒有用失效的卡打後端', async () => {
    renderWizard();
    submitNewPassword();

    expect(await screen.findByText(/密碼已更新。請用新密碼重新登入/)).toBeInTheDocument();
    expect(screen.getByText('步驟 2 / 7：管理員密碼')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /上一步/ })).toBeNull();
    expect(storedResume()).toMatchObject({ userId: ADMIN.id, stepId: 'api_keys' });

    const calls = backend.fetch.mock.calls.map(([url]) => new URL(String(url), 'http://touka.test').pathname);
    expect(calls.at(-1)).toBe(`/v1/admin/users/${ADMIN.id}`);
    expect(onExpired).not.toHaveBeenCalled();
    expect(logout).not.toHaveBeenCalled();
  });

  it('按「前往登入」才登出；登出進行中按鈕停用，不會連點', async () => {
    renderWizard();
    submitNewPassword();
    const goToLogin = await screen.findByRole('button', { name: '前往登入' });

    fireEvent.click(goToLogin);
    expect(logout).toHaveBeenCalledTimes(1);
    expect(goToLogin).toBeDisabled();
    fireEvent.click(goToLogin);
    expect(logout).toHaveBeenCalledTimes(1);
  });

  it('改密碼的回應晚到、畫面已被卸載（例如背景請求先觸發登入逾期）：接續點照樣記下', async () => {
    const release = backend.delayPatch();
    const view = renderWizard();
    submitNewPassword();
    await waitFor(() =>
      expect(backend.fetch.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === 'PATCH')).toBe(true));

    view.unmount();
    await act(async () => { release(); });

    await waitFor(() => expect(storedResume()).toMatchObject({ userId: ADMIN.id, stepId: 'api_keys' }));
  });

  it('改密碼失敗：顯示原本的錯誤，不記接續點、不要求重新登入', async () => {
    renderWizard();
    submitNewPassword('Rejected#Pass1');

    expect(await screen.findByText('密碼不合規定')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '前往登入' })).toBeNull();
    expect(storedResume()).toBeNull();
  });
});
