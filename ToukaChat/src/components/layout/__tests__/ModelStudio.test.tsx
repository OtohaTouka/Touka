/**
 * ModelStudio — 拿掉不會送出的取樣參數拉桿、不寫死基礎引擎（ADR-0250 A2）。
 *
 * 取樣參數三家都不送（ADR-0241 決策 2），拉桿沒有作用；API 欄位與存放值保留，存檔照原值送回。
 * 基礎引擎沒有值時用模型清單裡第一個一般模型（不列 is_agent），不再寫死舊模型名。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import React from 'react';

const { mockFetchTouka } = vi.hoisted(() => ({ mockFetchTouka: vi.fn() }));

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://localhost:8000',
  fetchTouka: mockFetchTouka,
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { ModelStudio } from '../ModelStudio';

const MODEL_LIST = [
  { id: 'Alice', is_agent: true },
  { id: 'm-first' },
  { id: 'm-second' },
];

let customModels: Record<string, unknown>[] = [];

function ok(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  customModels = [];
  mockFetchTouka.mockImplementation((_url: string, path: string) => {
    if (path === '/v1/models') return ok({ data: MODEL_LIST });
    if (path === '/v1/ui/custom_models') return ok({ models: customModels });
    if (path === '/v1/tools_stats') return ok({ tools: [] });
    return ok({});
  });
});

/** 等清單載入（基礎引擎選單出現選項）。 */
async function waitForLoaded() {
  await screen.findByRole('option', { name: 'm-first' });
}

/** 按 Save Config，回傳送出的 config。 */
async function saveAndReadConfig(): Promise<Record<string, unknown>> {
  fireEvent.click(screen.getByRole('button', { name: /Save Config/ }));
  await waitFor(() => expect(
    mockFetchTouka.mock.calls.some(([, path, opts]) =>
      String(path).startsWith('/v1/ui/custom_models/') && opts?.method === 'POST'),
  ).toBe(true));
  const call = mockFetchTouka.mock.calls.find(([, path, opts]) =>
    String(path).startsWith('/v1/ui/custom_models/') && opts?.method === 'POST')!;
  return JSON.parse(call[2].body as string).config;
}

describe('ModelStudio：沒有取樣參數拉桿（ADR-0250 A2）', () => {
  it('沒有 Temperature／Top P／Top K 拉桿，也沒有「推理參數」區塊', async () => {
    render(<ModelStudio />);
    await waitForLoaded();
    expect(screen.queryAllByRole('slider')).toHaveLength(0);
    expect(screen.queryByText('Temperature')).toBeNull();
    expect(screen.queryByText('Top P')).toBeNull();
    expect(screen.queryByText('Top K')).toBeNull();
    expect(screen.queryByText(/推理參數/)).toBeNull();
  });

  it('API 欄位照 ADR-0241 保留：存檔照原值送回 temperature／top_p／top_k', async () => {
    customModels = [{
      id: 'mine', name: 'mine', description: '', systemPrompt: '',
      config: { base_model: 'm-second', temperature: 0.3, top_p: 0.8, top_k: 12 },
    }];
    render(<ModelStudio />);
    await waitForLoaded();
    const config = await saveAndReadConfig();
    expect(config).toMatchObject({ temperature: 0.3, top_p: 0.8, top_k: 12 });
  });
});

describe('ModelStudio：基礎引擎不寫死（ADR-0250 A2）', () => {
  it('還沒有自訂模型時，新模型的基礎引擎是清單第一個一般模型', async () => {
    render(<ModelStudio />);
    await waitForLoaded();
    const config = await saveAndReadConfig();
    expect(config.base_model).toBe('m-first');
  });

  it('已存的自訂模型沒有基礎引擎時，用清單第一個一般模型', async () => {
    customModels = [{ id: 'mine', name: 'mine', description: '', systemPrompt: '', config: {} }];
    render(<ModelStudio />);
    await waitForLoaded();
    const config = await saveAndReadConfig();
    expect(config.base_model).toBe('m-first');
  });

  it('已存的基礎引擎照原值保留', async () => {
    customModels = [{
      id: 'mine', name: 'mine', description: '', systemPrompt: '', config: { base_model: 'm-second' },
    }];
    render(<ModelStudio />);
    await waitForLoaded();
    const config = await saveAndReadConfig();
    expect(config.base_model).toBe('m-second');
  });
});

describe('ModelStudio：基礎引擎選單和靈魂頁同一套規則', () => {
  it('不列代理、human 與舊靈魂路由；預設是第一個一般模型', async () => {
    const mixed = [
      { id: 'Alice', owned_by: 'proxy', is_agent: true },
      { id: 'Human', owned_by: 'human' },
      { id: 'ToukaSoulGhost', owned_by: 'touka' },
      { id: 'm-first', owned_by: 'openai' },
      { id: 'm-second', owned_by: 'anthropic' },
    ];
    mockFetchTouka.mockImplementation((_url: string, path: string) =>
      ok(path === '/v1/models' ? { data: mixed } : path === '/v1/ui/custom_models' ? { models: [] } : {}));
    render(<ModelStudio />);
    await waitForLoaded();
    const select = screen.getByRole('option', { name: 'm-first' }).closest('select')!;
    expect(within(select).getAllByRole('option').map(o => o.getAttribute('value')))
      .toEqual(['m-first', 'm-second']);
    expect((await saveAndReadConfig()).base_model).toBe('m-first');
  });
});

describe('ModelStudio：建立時撞名（成果審 R24）', () => {
  it('存檔被撞名擋下時顯示撞到的 ID，不說存好了', async () => {
    const { toast } = await import('sonner');
    mockFetchTouka.mockImplementation((_url: string, path: string, opts?: RequestInit) => {
      if (path === '/v1/models') return ok({ data: MODEL_LIST });
      if (path === '/v1/ui/custom_models') return ok({ models: [] });
      if (path === '/v1/tools_stats') return ok({ tools: [] });
      if (String(path).startsWith('/v1/ui/custom_models/') && opts?.method === 'POST') {
        return Promise.resolve(Response.json({ detail: {
          code: 'AGENT_ID_COLLECTION_COLLISION', id: 1416, collides_with: 'zz_taken',
        } }, { status: 409 }));
      }
      return ok({});
    });
    render(<ModelStudio />);
    await waitForLoaded();
    fireEvent.click(screen.getByRole('button', { name: /Save Config/ }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('zz_taken')));
    expect(toast.success).not.toHaveBeenCalled();
  });
});
