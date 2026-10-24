import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react';
import { SoulStudio } from '../SoulStudio';
import React from 'react';
import { ERRORS } from '@/lib/error_codes';
import i18n from '@/lib/i18n';
import { toast } from 'sonner';

// C4（soul-crud repair）：SoulStudio 詳情頁編輯模式＋刪除鈕＋admin 密碼確認 modal。
// 一個 soul 存在時渲染詳情頁（agents 空 → SoulStudio 不過濾、直接顯示原始 souls）。

const SOUL = {
  id: 'MySoul',
  persona: '原始人格',
  active_brain: 'claude-test',
  config: { base_model: 'claude-test', temperature: 0.7, top_p: 1, top_k: 40, enabled_tools: [] },
};

const { mockFetchTouka, mockRefreshAgents, mockProxy, agentState } = vi.hoisted(() => ({
  mockFetchTouka: vi.fn(),
  mockRefreshAgents: vi.fn(),
  mockProxy: vi.fn(() => 'http://localhost:8000'),
  agentState: { agents: [] as Array<{ id: string; type: string }> },
}));

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: mockProxy,
  getActiveConnection: () => null,
  fetchTouka: mockFetchTouka,
}));
vi.mock('@/lib/providers/AgentProvider', () => ({
  useAgents: () => ({ agents: agentState.agents, refreshAgents: mockRefreshAgents }),
}));

function baseImpl(_proxy: string, path: string, opts?: RequestInit) {
  if (opts?.method === 'POST' || opts?.method === 'DELETE') {
    return Promise.resolve(Response.json({ status: 'success', soul_id: 'MySoul' }));
  }
  if (path === '/v1/models') {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [{ id: 'claude-test' }] }) });
  }
  if (path === '/v1/tools_stats') {
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ builtin_schemas: [], memory_schemas: [], plugin_schemas: [], mcp_schemas: [] }),
    });
  }
  // /v1/souls GET → one soul; POST/DELETE → success
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ souls: [SOUL] }) });
}

describe('SoulStudio — edit/delete with admin step-up', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRefreshAgents.mockResolvedValue(true);
    mockFetchTouka.mockImplementation(baseImpl);
  });

  async function renderDetail() {
    render(<SoulStudio />);
    // 詳情頁標題（大寫 soul id）出現＝已離開空狀態
    await screen.findByRole('heading', { name: /MySoul/i });
  }

  it('detail view exposes Edit and Delete controls', async () => {
    await renderDetail();
    expect(screen.getByTitle('編輯靈魂')).toBeInTheDocument();
    expect(screen.getByTitle('刪除靈魂')).toBeInTheDocument();
  });

  it('Delete → password modal → DELETE /v1/souls/{id} with admin_password in body', async () => {
    await renderDetail();
    fireEvent.click(screen.getByTitle('刪除靈魂'));

    const pw = await screen.findByPlaceholderText(/管理員密碼|admin password/i);
    fireEvent.change(pw, { target: { value: 'SecretPass' } });
    fireEvent.click(screen.getByRole('button', { name: /確認刪除|confirm delete/i }));

    await waitFor(() => {
      const call = mockFetchTouka.mock.calls.find(
        ([, path, opts]) => path === '/v1/souls/MySoul' && (opts as RequestInit)?.method === 'DELETE',
      );
      expect(call).toBeDefined();
      const body = JSON.parse((call![2] as RequestInit).body as string);
      expect(body.admin_password).toBe('SecretPass');
    });
  });

  it('刪除確認框提醒還有備份，以及刪除前不會自動存快照', async () => {
    await renderDetail();
    fireEvent.click(screen.getByTitle('刪除靈魂'));
    expect(await screen.findByText(/「MySoul」的資料可能還留在之前的系統快照裡/)).toBeInTheDocument();
    expect(screen.getByText(/Touka_Backup_/)).toBeInTheDocument();
    expect(screen.getByText(/vault\/core_memory_backups/)).toBeInTheDocument();
    expect(screen.getByText(/刪除前不會自動存快照/)).toBeInTheDocument();
    expect(screen.getByText(/刪整份快照/)).toBeInTheDocument();
  });

  it('刪除完成的訊息也提醒還有備份', async () => {
    const success = vi.spyOn(toast, 'success');
    await renderDetail();
    fireEvent.click(screen.getByTitle('刪除靈魂'));
    fireEvent.change(await screen.findByPlaceholderText(/管理員密碼|admin password/i), { target: { value: 'pw' } });
    fireEvent.click(screen.getByRole('button', { name: /確認刪除|confirm delete/i }));
    await waitFor(() => expect(success).toHaveBeenCalledWith(
      expect.stringMatching(/MySoul/),
      expect.objectContaining({ description: expect.stringMatching(/「MySoul」的資料可能還留在之前的系統快照裡/) }),
    ));
  });

  it('還有記憶時刪除被擋：顯示「請先重置」的引導，靈魂留在畫面上', async () => {
    const error = vi.spyOn(toast, 'error');
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) =>
      opts?.method === 'DELETE'
        ? Promise.resolve(Response.json(
          { detail: { code: 'SOUL_MEMORY_NOT_RESET', id: ERRORS.SOUL_MEMORY_NOT_RESET.id, facts: 2 } },
          { status: 409 },
        ))
        : baseImpl(proxy, path, opts));
    await renderDetail();
    fireEvent.click(screen.getByTitle('刪除靈魂'));
    fireEvent.change(await screen.findByPlaceholderText(/管理員密碼|admin password/i), { target: { value: 'pw' } });
    fireEvent.click(screen.getByRole('button', { name: /確認刪除|confirm delete/i }));

    await waitFor(() => expect(error).toHaveBeenCalledWith(expect.stringMatching(/還有記憶.*重置/)));
    expect(screen.getByRole('heading', { name: /MySoul/i })).toBeInTheDocument();
  });

  it('隔離家目錄清不掉時刪除被擋：照錯誤碼顯示原因（i18n），靈魂留在畫面上', async () => {
    const error = vi.spyOn(toast, 'error');
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) =>
      opts?.method === 'DELETE'
        ? Promise.resolve(Response.json(
          { detail: { code: 'SOUL_SANDBOX_HOME_NOT_CLEARED', id: ERRORS.SOUL_SANDBOX_HOME_NOT_CLEARED.id } },
          { status: 503 },
        ))
        : baseImpl(proxy, path, opts));
    await renderDetail();
    fireEvent.click(screen.getByTitle('刪除靈魂'));
    fireEvent.change(await screen.findByPlaceholderText(/管理員密碼|admin password/i), { target: { value: 'pw' } });
    fireEvent.click(screen.getByRole('button', { name: /確認刪除|confirm delete/i }));

    await waitFor(() => expect(error).toHaveBeenCalledWith(i18n.t('error.soul.sandbox_home_not_cleared')));
    expect(i18n.t('error.soul.sandbox_home_not_cleared')).toMatch(/執行器.*沒有刪除/);
    expect(screen.getByRole('heading', { name: /MySoul/i })).toBeInTheDocument();
  });

  it('does NOT call DELETE if password modal is cancelled', async () => {
    await renderDetail();
    fireEvent.click(screen.getByTitle('刪除靈魂'));
    await screen.findByPlaceholderText(/管理員密碼|admin password/i);
    fireEvent.click(screen.getByRole('button', { name: /取消|cancel/i }));

    // give any erroneous async a tick
    await new Promise((r) => setTimeout(r, 0));
    const del = mockFetchTouka.mock.calls.find(
      ([, path, opts]) => path === '/v1/souls/MySoul' && (opts as RequestInit)?.method === 'DELETE',
    );
    expect(del).toBeUndefined();
  });

  it('Edit → persona becomes editable → Save → password modal → POST with persona + admin_password', async () => {
    await renderDetail();
    fireEvent.click(screen.getByTitle('編輯靈魂'));

    // persona textarea 現在可編輯（非 readOnly）
    const textarea = screen.getByDisplayValue('原始人格') as HTMLTextAreaElement;
    expect(textarea.readOnly).toBe(false);
    fireEvent.change(textarea, { target: { value: '新的人格' } });

    fireEvent.click(screen.getByRole('button', { name: /儲存變更|save changes/i }));
    const pw = await screen.findByPlaceholderText(/管理員密碼|admin password/i);
    fireEvent.change(pw, { target: { value: 'EditPass' } });
    fireEvent.click(screen.getByRole('button', { name: /確認儲存|confirm save/i }));

    await waitFor(() => {
      const call = mockFetchTouka.mock.calls.find(
        ([, path, opts]) => path === '/v1/souls/MySoul' && (opts as RequestInit)?.method === 'POST',
      );
      expect(call).toBeDefined();
      const body = JSON.parse((call![2] as RequestInit).body as string);
      expect(body.persona).toBe('新的人格');
      expect(body.admin_password).toBe('EditPass');
    });
  });
});

describe('SoulStudio — 保存結果必須可見', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockProxy.mockReturnValue('http://localhost:8000');
    agentState.agents = [];
    mockRefreshAgents.mockResolvedValue(true);
    mockFetchTouka.mockImplementation(baseImpl);
  });

  async function edit() {
    const view = render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    fireEvent.click(screen.getByTitle('編輯靈魂'));
    fireEvent.change(screen.getByDisplayValue('原始人格'), { target: { value: '修訂後人格' } });
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/i }));
    const password = await screen.findByPlaceholderText(/管理員密碼/i);
    fireEvent.change(password, { target: { value: 'SyntheticPassword' } });
    return { ...view, password, confirm: screen.getByRole('button', { name: /確認儲存/i }) };
  }

  function respond(response: Response) {
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) =>
      opts?.method === 'POST' ? Promise.resolve(response) : baseImpl(proxy, path, opts));
  }

  it('canonical 保存成功後關閉密碼框，返回同魂唯讀頁並持續顯示成功', async () => {
    const { confirm } = await edit();
    // The original POST is authoritative; subsequent refresh has no new result yet.
    mockRefreshAgents.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(confirm);
    expect(await screen.findByRole('status')).toHaveTextContent('密碼驗證通過，修改已儲存');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByDisplayValue('修訂後人格')).toHaveAttribute('readonly');
    expect(screen.getByRole('heading', { name: /MySoul/i })).toBeInTheDocument();
  });

  it('密碼錯誤在視窗內顯示，清密碼但保留草稿', async () => {
    const { password, confirm } = await edit();
    respond(Response.json({ detail: { code: 'AUTH_REAUTH_FAILED', id: ERRORS.AUTH_REAUTH_FAILED.id } }, { status: 403 }));
    fireEvent.click(confirm);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(password).toHaveValue('');
    expect(password).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByDisplayValue('修訂後人格')).not.toHaveAttribute('readonly');
  });

  it.each([
    [403, { detail: { code: 'AUTH_ADMIN_REQUIRED', id: ERRORS.AUTH_ADMIN_REQUIRED.id } }],
    [422, { detail: [{ loc: ['body', 'config'], msg: 'invalid field', type: 'value_error' }] }],
    [500, { detail: { code: 'SOUL_WRITE_FAILED', id: 1407 } }],
  ])('HTTP %s 錯誤留在框內，不能誤認為密碼錯誤', async (status, body) => {
    const { password, confirm } = await edit();
    respond(Response.json(body, { status }));
    fireEvent.click(confirm);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(password).toHaveValue('');
    expect(password).not.toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByDisplayValue('修訂後人格')).not.toHaveAttribute('readonly');
  });

  it.each([
    { status: 'success', soul_id: 'OtherSoul' },
    { status: 'pending', soul_id: 'MySoul' },
    { souls: [] },
  ])('不把非 canonical 的 2xx 當保存成功：%j', async (body) => {
    const { confirm } = await edit();
    respond(Response.json(body));
    fireEvent.click(confirm);
    expect(await screen.findByRole('alert')).toHaveTextContent('尚無法確認是否已儲存');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('保存成功但 refreshAgents 失敗只顯示同步警告，重讀不再次 POST', async () => {
    const { confirm } = await edit();
    mockRefreshAgents.mockResolvedValue(false);
    fireEvent.click(confirm);
    expect(await screen.findByRole('status')).toHaveTextContent('密碼驗證通過，修改已儲存');
    expect(await screen.findByRole('alert')).toHaveTextContent('修改已儲存，但重新整理未完成');
    const before = mockFetchTouka.mock.calls.filter(([, , opts]) => opts?.method === 'POST').length;
    fireEvent.click(screen.getByRole('button', { name: /重新讀取/i }));
    await waitFor(() => expect(mockRefreshAgents.mock.calls.length).toBeGreaterThan(1));
    expect(mockFetchTouka.mock.calls.filter(([, , opts]) => opts?.method === 'POST')).toHaveLength(before);
  });

  it('同一事件批次的 click 與 Enter 只送一次保存', async () => {
    const { confirm, password } = await edit();
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) =>
      opts?.method === 'POST' ? new Promise(() => {}) : baseImpl(proxy, path, opts));
    act(() => {
      fireEvent.click(confirm);
      fireEvent.keyDown(password, { key: 'Enter' });
    });
    expect(mockFetchTouka.mock.calls.filter(([, , opts]) => opts?.method === 'POST')).toHaveLength(1);
    expect(confirm).toBeDisabled();
    expect(screen.getByRole('button', { name: /取消/i })).toBeDisabled();
  });

  it('未完成 POST 在局部期限後退出 busy，明示結果未知且不重送', async () => {
    const { confirm } = await edit();
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) =>
      opts?.method === 'POST' ? new Promise(() => {}) : baseImpl(proxy, path, opts));
    vi.useFakeTimers();
    try {
      fireEvent.click(confirm);
      await act(async () => { await vi.advanceTimersByTimeAsync(30_001); });
      expect(screen.getByRole('alert')).toHaveTextContent('尚無法確認是否已儲存');
      expect(screen.getByRole('button', { name: /取消/i })).not.toBeDisabled();
      expect(mockFetchTouka.mock.calls.filter(([, , opts]) => opts?.method === 'POST')).toHaveLength(1);
    } finally { vi.useRealTimers(); }
  });

  it('成功 headers 但 body 未完成不能先成功，期限後顯示未知', async () => {
    const { confirm } = await edit();
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) =>
      opts?.method === 'POST' ? Promise.resolve({ ok: true, json: () => new Promise(() => {}) })
        : baseImpl(proxy, path, opts));
    vi.useFakeTimers();
    try {
      fireEvent.click(confirm);
      await act(async () => { await vi.advanceTimersByTimeAsync(30_001); });
      expect(screen.getByRole('alert')).toHaveTextContent('尚無法確認是否已儲存');
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it.each([
    { detail: { code: 'SOUL_WRITE_FAILED', id: -1 } },
    { detail: { code: 'AUTH_REAUTH_FAILED', id: ERRORS.AUTH_REAUTH_FAILED.id } },
    { detail: 'untrusted backend message' },
  ])('不合法或衝突的錯誤 envelope 顯示結果未知：%j', async (body) => {
    const { confirm } = await edit();
    respond(Response.json(body, { status: 500 }));
    fireEvent.click(confirm);
    expect(await screen.findByRole('alert')).toHaveTextContent('尚無法確認是否已儲存');
    expect(screen.getByRole('alert')).not.toHaveTextContent('untrusted backend message');
  });

  it('unmount 後完成的舊 POST 不顯示成功 toast', async () => {
    const { confirm, unmount } = await edit();
    let complete!: (response: Response) => void;
    const pending = new Promise<Response>(resolve => { complete = resolve; });
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) =>
      opts?.method === 'POST' ? pending : baseImpl(proxy, path, opts));
    const success = vi.spyOn(toast, 'success');
    try {
      fireEvent.click(confirm);
      unmount();
      await act(async () => { complete(Response.json({ status: 'success', soul_id: 'MySoul' })); });
      expect(success).not.toHaveBeenCalled();
    } finally { success.mockRestore(); }
  });

  it('成功提示在捲動區外，關框後焦點回編輯鈕且保留原捲動', async () => {
    const view = render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    const scroll = view.container.querySelector('.overflow-y-auto') as HTMLElement;
    scroll.scrollTop = 420;
    fireEvent.click(screen.getByTitle('編輯靈魂'));
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/i }));
    fireEvent.change(await screen.findByPlaceholderText(/管理員密碼/i), { target: { value: 'synthetic' } });
    mockRefreshAgents.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(screen.getByRole('button', { name: /確認儲存/i }));
    const status = await screen.findByRole('status');
    expect(scroll.contains(status)).toBe(false);
    await waitFor(() => expect(screen.getByTitle('編輯靈魂')).toHaveFocus());
    expect(scroll.scrollTop).toBe(420);
  });

  it.each(['touka_connections_updated', 'storage'])('Proxy %s 通知立即清密碼並作廢 pending POST', async eventName => {
    const { confirm, password } = await edit();
    let complete!: (response: Response) => void;
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) =>
      opts?.method === 'POST' ? new Promise<Response>(resolve => { complete = resolve; }) : baseImpl(proxy, path, opts));
    fireEvent.click(confirm);
    mockProxy.mockReturnValue('http://other.invalid');
    act(() => {
      window.dispatchEvent(eventName === 'storage' ? new StorageEvent('storage', { key: 'touka_connections' }) : new Event(eventName));
    });
    expect(password).toHaveValue('');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await act(async () => { complete(Response.json({ status: 'success', soul_id: 'MySoul' })); });
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(mockFetchTouka.mock.calls.some(([proxy, path]) => proxy === 'http://other.invalid' && path === '/v1/souls')).toBe(true);
  });

  it.each(['原始人格', '修訂後人格'])('未知结果重讀顯示與草稿核對：server=%s；不自動再 POST', async persona => {
    const { confirm, password } = await edit();
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) => {
      if (opts?.method === 'POST') return Promise.reject(new TypeError('synthetic network lost'));
      if (path === '/v1/souls') return Promise.resolve(Response.json({ souls: [{ ...SOUL, persona }] }));
      return baseImpl(proxy, path, opts);
    });
    fireEvent.click(confirm);
    await screen.findByRole('alert');
    fireEvent.change(password, { target: { value: 'new synthetic password' } });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: /重新讀取/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(
      persona === '修訂後人格' ? '伺服器內容與草稿一致' : '與草稿不同的欄位：靈魂核心',
    ));
    expect(screen.getByRole('alert')).toHaveTextContent('不代表密碼已通過驗證');
    expect(screen.getByDisplayValue('修訂後人格')).not.toHaveAttribute('readonly');
    expect(confirm).not.toBeDisabled();
    expect(mockFetchTouka.mock.calls.filter(([, , opts]) => opts?.method === 'POST')).toHaveLength(1);
  });

  it.each(['known', 'unknown'] as const)('取消再開保留草稿：%s 結果只清已知錯誤，未知仍鎖住', async mode => {
    const { confirm } = await edit();
    respond(mode === 'known'
      ? Response.json({ detail: { code: 'AUTH_REAUTH_FAILED', id: ERRORS.AUTH_REAUTH_FAILED.id } }, { status: 403 })
      : Response.json({ status: 'pending' }));
    fireEvent.click(confirm);
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: /取消/ }));
    expect(screen.getByDisplayValue('修訂後人格')).not.toHaveAttribute('readonly');
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/i }));
    fireEvent.change(await screen.findByPlaceholderText(/管理員密碼/i), { target: { value: 'again synthetic' } });
    if (mode === 'known') {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: /確認儲存/i })).not.toBeDisabled();
    } else {
      expect(screen.getByRole('alert')).toHaveTextContent('尚無法確認是否已儲存');
      expect(screen.getByRole('button', { name: /確認儲存/i })).toBeDisabled();
    }
  });

  it.each(['missing', 'duplicate'] as const)('重新讀取 %s 目標不能解鎖未知結果', async mode => {
    const { confirm, password } = await edit();
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) => {
      if (opts?.method === 'POST') return Promise.reject(new TypeError('synthetic connection lost'));
      if (path === '/v1/souls') return Promise.resolve(Response.json({ souls: mode === 'missing' ? [] : [SOUL, SOUL] }));
      return baseImpl(proxy, path, opts);
    });
    fireEvent.click(confirm);
    await screen.findByRole('alert');
    fireEvent.change(password, { target: { value: 'new synthetic' } });
    fireEvent.click(screen.getByRole('button', { name: /重新讀取/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('重新讀取未完成'));
    expect(confirm).toBeDisabled();
    expect(screen.getByDisplayValue('修訂後人格')).not.toHaveAttribute('readonly');
  });

  it('Proxy storage 通知後舊列表不能再編輯，須先取得新 Proxy 同目標內容', async () => {
    await edit();
    let complete!: (response: Response) => void;
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) =>
      proxy === 'http://other.invalid' && path === '/v1/souls'
        ? new Promise<Response>(resolve => { complete = resolve; }) : baseImpl(proxy, path, opts));
    mockProxy.mockReturnValue('http://other.invalid');
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'touka_connections' })));
    expect(screen.queryByTitle('編輯靈魂')).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue('原始人格')).not.toBeInTheDocument();
    await waitFor(() => expect(complete).toBeDefined());
    await act(async () => { complete(Response.json({ souls: [{ ...SOUL, persona: '新 Proxy 人格' }] })); });
    fireEvent.click(screen.getByTitle('編輯靈魂'));
    expect(screen.getByDisplayValue('新 Proxy 人格')).not.toHaveAttribute('readonly');
  });

  it('保存前普通 GET 晚到不能覆蓋已確認保存值', async () => {
    let completeOld!: (response: Response) => void;
    let reads = 0;
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) => {
      if (path === '/v1/souls' && !opts?.method && ++reads === 1) {
        return new Promise<Response>(resolve => { completeOld = resolve; });
      }
      return baseImpl(proxy, path, opts);
    });
    const view = render(<SoulStudio />);
    agentState.agents = [{ id: SOUL.id, type: 'soul' }];
    view.rerender(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    fireEvent.click(screen.getByTitle('編輯靈魂'));
    fireEvent.change(screen.getByDisplayValue('原始人格'), { target: { value: '已確認的新值' } });
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/i }));
    fireEvent.change(await screen.findByPlaceholderText(/管理員密碼/i), { target: { value: 'synthetic' } });
    mockRefreshAgents.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(screen.getByRole('button', { name: /確認儲存/i }));
    await screen.findByRole('status');
    await act(async () => { completeOld(Response.json({ souls: [SOUL] })); });
    expect(screen.getByDisplayValue('已確認的新值')).toHaveAttribute('readonly');
    expect(screen.queryByDisplayValue('原始人格')).not.toBeInTheDocument();
  });
});

// ADR-0250：主要模型分「你指定的」（config.base_model）與「目前在用的」（active_brain）兩欄。
describe('SoulStudio — 你指定的模型／目前在用的模型', () => {
  const MODELS = [
    { id: 'claude-chosen-y', owned_by: 'anthropic' },
    { id: 'claude-switched-x', owned_by: 'anthropic' },
    { id: 'MySoul', owned_by: 'proxy', is_agent: true },
    { id: 'Human', owned_by: 'human' },
  ];

  function serve(soul: Record<string, unknown>) {
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) => {
      if (opts?.method === 'POST') return Promise.resolve(Response.json({ status: 'success', soul_id: 'MySoul' }));
      if (path === '/v1/models') return Promise.resolve(Response.json({ data: MODELS }));
      if (path === '/v1/souls') return Promise.resolve(Response.json({ souls: [soul] }));
      return baseImpl(proxy, path, opts);
    });
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mockProxy.mockReturnValue('http://localhost:8000');
    agentState.agents = [];
    mockRefreshAgents.mockResolvedValue(true);
  });

  const group = (name: string) => screen.getByRole('group', { name });

  const SWITCHED = { ...SOUL, active_brain: 'claude-switched-x', config: { ...SOUL.config, base_model: 'claude-chosen-y' } };

  it('只改人格存檔時送回「你指定的」，不是模型自己換的那個（H2 情境 1）', async () => {
    serve(SWITCHED);
    render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    fireEvent.click(screen.getByTitle('編輯靈魂'));
    fireEvent.change(screen.getByDisplayValue('原始人格'), { target: { value: '只改人格' } });
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/i }));
    fireEvent.change(await screen.findByPlaceholderText(/管理員密碼/i), { target: { value: 'synthetic' } });
    fireEvent.click(screen.getByRole('button', { name: /確認儲存/i }));
    await waitFor(() => {
      const call = mockFetchTouka.mock.calls.find(([, path, opts]) => path === '/v1/souls/MySoul' && opts?.method === 'POST');
      expect(JSON.parse((call![2] as RequestInit).body as string).config.base_model).toBe('claude-chosen-y');
    });
  });

  it('兩欄分開顯示，下拉的值是「你指定的」、只列一般模型', async () => {
    serve(SWITCHED);
    render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    expect(within(group('你指定的模型')).getByText('claude-chosen-y')).toBeInTheDocument();
    expect(within(group('目前在用的模型')).getByText('claude-switched-x')).toBeInTheDocument();
    expect(within(group('目前在用的模型')).getByText(/和你指定的不同/)).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('編輯靈魂'));
    const select = screen.getByRole('combobox', { name: '你指定的模型' });
    await waitFor(() => expect(within(select).getAllByRole('option').map(o => o.getAttribute('value')))
      .toEqual(['claude-chosen-y', 'claude-switched-x']));
    expect(select).toHaveValue('claude-chosen-y');
  });

  it('從沒指定過模型時，不顯示「模型自己換過」的說明', async () => {
    serve({ ...SOUL, active_brain: 'claude-switched-x', config: { enabled_tools: [] } });
    render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    expect(within(group('你指定的模型')).getByText('未配置')).toBeInTheDocument();
    expect(within(group('目前在用的模型')).getByText('claude-switched-x')).toBeInTheDocument();
    expect(within(group('目前在用的模型')).queryByText(/和你指定的不同/)).not.toBeInTheDocument();
  });

  it('沒設模型時兩欄都顯示翻譯過的「未配置」，下拉的佔位選項不能選', async () => {
    serve({ ...SOUL, active_brain: null, config: { enabled_tools: [] } });
    render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    expect(within(group('你指定的模型')).getByText('未配置')).toBeInTheDocument();
    expect(within(group('目前在用的模型')).getByText('未配置')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('編輯靈魂'));
    const select = screen.getByRole('combobox', { name: '你指定的模型' });
    expect(select).toHaveValue('');
    const placeholder = within(select).getByRole('option', { name: '未配置' });
    expect(placeholder).toBeDisabled();
  });

  it('唯讀與編輯模式都沒有取樣參數拉桿', async () => {
    serve(SOUL);
    render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    expect(screen.queryByText('Temperature')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle('編輯靈魂'));
    expect(screen.queryByText('Temperature')).not.toBeInTheDocument();
    expect(screen.queryByText('Top P')).not.toBeInTheDocument();
    expect(screen.queryByText('Top K')).not.toBeInTheDocument();
  });
});

// ADR-0281 T2：建立與編輯共用同一個工具勾選器；MCP 存 `伺服器/工具`；外掛不列；看不到的條目保留。
describe('SoulStudio — 工具清單', () => {
  const STATS = {
    builtin_schemas: [{ function: { name: 'read' } }],
    memory_schemas: [],
    plugin_schemas: [{ function: { name: 'my_plugin' } }],
    mcp_schemas: [{ function: { name: 'lookup', raw_name: 'srv__lookup' }, server: 'srv', tool: 'lookup' }],
  };

  function serveTools(enabledTools: string[]) {
    const soul = { ...SOUL, config: { ...SOUL.config, enabled_tools: enabledTools } };
    mockFetchTouka.mockImplementation((proxy: string, path: string, opts?: RequestInit) => {
      if (opts?.method) return baseImpl(proxy, path, opts);
      if (path === '/v1/tools_stats') return Promise.resolve(Response.json(STATS));
      if (path === '/v1/souls') return Promise.resolve(Response.json({ souls: [soul] }));
      return baseImpl(proxy, path, opts);
    });
  }

  function sentConfig(path: string) {
    const call = mockFetchTouka.mock.calls.find(([, p, opts]) => p === path && opts?.method === 'POST');
    return call ? JSON.parse((call[2] as RequestInit).body as string).config : undefined;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mockProxy.mockReturnValue('http://localhost:8000');
    agentState.agents = [];
    mockRefreshAgents.mockResolvedValue(true);
  });

  it('唯讀頁只列勾了的工具，看不到的條目標「目前不在清單」', async () => {
    serveTools(['srv__lookup', 'gone/tool']);
    render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });

    expect(await screen.findByText('srv/lookup')).toBeInTheDocument();
    expect(screen.queryByText('read')).not.toBeInTheDocument();
    expect(screen.getByText(/gone\/tool/)).toHaveTextContent('目前不在清單');
    expect(screen.getByText('2 個啟用')).toBeInTheDocument();
  });

  it('編輯時能改工具清單：存 `伺服器/工具`，保留看不到的條目，沒有外掛', async () => {
    serveTools(['read', 'gone/tool']);
    render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    fireEvent.click(screen.getByTitle('編輯靈魂'));

    fireEvent.click(await screen.findByRole('button', { name: 'srv/lookup' }));
    expect(screen.queryByRole('button', { name: 'my_plugin' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/i }));
    fireEvent.change(await screen.findByPlaceholderText(/管理員密碼/i), { target: { value: 'synthetic' } });
    fireEvent.click(screen.getByRole('button', { name: /確認儲存/i }));

    await waitFor(() => expect(sentConfig('/v1/souls/MySoul')?.enabled_tools)
      .toEqual(['read', 'gone/tool', 'srv/lookup']));
  });

  it('建立靈魂時用同一個勾選器，存 `伺服器/工具`', async () => {
    serveTools([]);
    render(<SoulStudio />);
    await screen.findByRole('heading', { name: /MySoul/i });
    fireEvent.click(screen.getByTitle('新增核心'));
    const dialog = await screen.findByRole('dialog');

    fireEvent.click(await within(dialog).findByRole('button', { name: 'srv/lookup' }));
    expect(within(dialog).queryByRole('button', { name: 'my_plugin' })).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: /啟動靈魂核心/i }));

    await waitFor(() => expect(sentConfig('/v1/souls')?.enabled_tools).toEqual(['srv/lookup']));
  });
});
