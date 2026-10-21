import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { SoulStudio } from '../SoulStudio';
import React from 'react';

// ---------------------------------------------------------------------------
// vi.hoisted ensures mock variables are initialized before vi.mock runs
// ---------------------------------------------------------------------------
const { mockFetchTouka, mockRefreshAgents } = vi.hoisted(() => {
  const mockFetchTouka = vi.fn((_proxyUrl: string, path: string, _opts?: RequestInit) => {
    if (path === '/v1/models') {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ data: [{ id: 'claude-3-5-sonnet' }] }),
      });
    }
    if (path === '/v1/tools_stats') {
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({ builtin_schemas: [], memory_schemas: [], plugin_schemas: [], mcp_schemas: [] }),
      });
    }
    // Default: /v1/souls (GET + POST), etc.
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ souls: [] }),
    });
  });
  const mockRefreshAgents = vi.fn().mockResolvedValue(true);
  return { mockFetchTouka, mockRefreshAgents };
});

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://localhost:8000',
  getActiveConnection: () => null,
  fetchTouka: mockFetchTouka,
}));

vi.mock('@/lib/providers/AgentProvider', () => ({
  useAgents: () => ({
    agents: [],
    refreshAgents: mockRefreshAgents,
  }),
}));

describe('SoulStudio', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Restore implementation after vi.clearAllMocks in case it wiped it
    mockFetchTouka.mockImplementation((_proxyUrl: string, path: string, _opts?: RequestInit) => {
      if (path === '/v1/models') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ data: [{ id: 'claude-3-5-sonnet' }] }),
        });
      }
      if (path === '/v1/tools_stats') {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({ builtin_schemas: [], memory_schemas: [], plugin_schemas: [], mcp_schemas: [] }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ souls: [] }),
      });
    });
  });

  it('renders empty state when no souls found', async () => {
    render(<SoulStudio />);
    const elements = await screen.findAllByText(/未偵測到系統核心靈魂/);
    expect(elements.length).toBeGreaterThan(0);
  });

  it('shows modal when clicking add button in empty state', async () => {
    render(<SoulStudio />);

    const addButton = await screen.findByTitle('新增核心');
    fireEvent.click(addButton);

    const modalTitle = await screen.findByText(/燈火核心工作室/i);
    expect(modalTitle).toBeInTheDocument();
  });

  it('create-soul dialog has no sampling sliders (ADR-0241: never sent) but keeps the safety settings', async () => {
    render(<SoulStudio />);

    const addButton = await screen.findByTitle('新增核心');
    fireEvent.click(addButton);
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).queryByText('Temperature')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Top P')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Top K')).not.toBeInTheDocument();
    // Shared safety-settings grid is rendered inside the create form.
    expect(within(dialog).getByText(/騷擾訊息/)).toBeInTheDocument();
    expect(within(dialog).getByText(/危險行為/)).toBeInTheDocument();
  });

  it('create-soul model list only offers ordinary models and defaults to the first one (no hard-coded model)', async () => {
    mockFetchTouka.mockImplementation((_proxyUrl: string, path: string) => {
      if (path === '/v1/models') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ data: [
            { id: 'Alice', owned_by: 'proxy', is_agent: true },
            { id: 'MyCustom', owned_by: 'custom', is_agent: true },
            { id: 'ToukaSoulGhost', owned_by: 'touka' },
            { id: 'Human', owned_by: 'human' },
            { id: 'gpt-first', owned_by: 'openai' },
            { id: 'claude-4.6-sonnet', owned_by: 'anthropic' },
          ] }),
        });
      }
      if (path === '/v1/tools_stats') {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({ builtin_schemas: [], memory_schemas: [], plugin_schemas: [], mcp_schemas: [] }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ souls: [] }) });
    });
    render(<SoulStudio />);
    fireEvent.click(await screen.findByTitle('新增核心'));
    const dialog = await screen.findByRole('dialog');
    const select = await within(dialog).findByRole('combobox');
    await waitFor(() => expect(select).toHaveValue('gpt-first'));
    expect(within(select).getAllByRole('option').map(option => option.getAttribute('value')))
      .toEqual(['gpt-first', 'claude-4.6-sonnet']);

    fireEvent.change(within(dialog).getByPlaceholderText(/例如: Touka_Assistant/), { target: { value: 'NewSoul' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /啟動靈魂核心/i }));
    await waitFor(() => {
      const post = mockFetchTouka.mock.calls.find(
        ([, path, init]) => path === '/v1/souls' && (init as RequestInit | undefined)?.method === 'POST',
      );
      expect(JSON.parse((post![2] as RequestInit).body as string).config.base_model).toBe('gpt-first');
    });
  });

  it('create-soul dialog stays viewport-fixed & centered with a viewport-relative max-height', async () => {
    // Regression guard for the "Add Soul" UI anomalies:
    // (1) height: the dialog must NOT be capped at a tiny fixed height
    //     (max-h-80 = 320px) — the SystemPromptEditor child alone is 384px tall,
    //     so a 320px cap clips the form into an unusable sliver.
    // (2) centering: a leftover `relative` in the caller className silently
    //     overrode Radix's base `fixed` (tailwind-merge treats fixed/relative as
    //     one conflict group, last-wins), dropping viewport-centering so the
    //     modal shifted right and its right half went off-screen.
    render(<SoulStudio />);

    const addButton = await screen.findByTitle('新增核心');
    fireEvent.click(addButton);
    await screen.findByText(/燈火核心工作室/i);

    const dialog = screen.getByRole('dialog');
    // (1) height cap is viewport-relative, not the crushing fixed 320px.
    expect(dialog.className).not.toContain('max-h-80');
    expect(dialog.className).toContain('max-h-[85vh]');
    // (2) Radix's `fixed` centering must survive the merge; no stray `relative`.
    expect(dialog.className).toContain('fixed');
    expect(dialog.className).not.toMatch(/\brelative\b/);
  });

  it('dispatches touka_refresh_models after a successful create (new ToukaSoul must appear in the model selector)', async () => {
    // C2（soul-crud repair）BUG3 前端半：後端建立成功但 ModelProvider 不刷新 →
    // 新 ToukaSoul<id> 不出現在聊天模型選單、選不到＝「存檔後仍不能說話」。
    // ModelProvider 監聽 window 的 touka_refresh_models 事件（ModelProvider.tsx）。
    const onRefresh = vi.fn();
    window.addEventListener('touka_refresh_models', onRefresh);
    try {
      render(<SoulStudio />);

      const addButton = await screen.findByTitle('新增核心');
      fireEvent.click(addButton);
      const input = await screen.findByPlaceholderText(/例如: Touka_Assistant/);
      fireEvent.change(input, { target: { value: 'RefreshSoul' } });
      const submitButton = await screen.findByRole('button', { name: /啟動靈魂核心/i });
      fireEvent.click(submitButton);

      await waitFor(() => expect(onRefresh).toHaveBeenCalled(), { timeout: 10000 });
    } finally {
      window.removeEventListener('touka_refresh_models', onRefresh);
    }
  });

  it('submits POST to /v1/souls when form is filled and button clicked', async () => {
    render(<SoulStudio />);

    // Wait for empty-state to load, then open modal
    const addButton = await screen.findByTitle('新增核心');
    fireEvent.click(addButton);

    // Fill in Agent ID
    const input = await screen.findByPlaceholderText(/例如: Touka_Assistant/);
    fireEvent.change(input, { target: { value: 'NewSoul' } });

    // Wait for the create button to be ENABLED (models must finish loading)
    const submitButton = await screen.findByRole('button', { name: /啟動靈魂核心/i });
    expect(submitButton).not.toBeDisabled();
    fireEvent.click(submitButton);

    // Verify POST to /v1/souls was made — more reliable than checking side-effects.
    // Timeout is generous to handle concurrent test-suite resource contention.
    await waitFor(
      () => {
        const postCall = mockFetchTouka.mock.calls.find(
          ([, path, opts]) =>
            path === '/v1/souls' && (opts as RequestInit | undefined)?.method === 'POST',
        );
        expect(postCall).toBeDefined();
      },
      { timeout: 10000 },
    );
  });
});
