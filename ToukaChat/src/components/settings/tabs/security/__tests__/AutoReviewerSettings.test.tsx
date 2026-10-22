/**
 * Auto 審查員模型選單（ADR-0246 U2c-6、決定 5）。
 *
 * 選項只來自後端「已設定金鑰的模型」清單、預設未設定；建議值只是提示文字；說明文字照
 * 通用寫法（不得寫成只送某一家廠商）。用真的 zh-TW 翻譯斷言文字。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import { AutoReviewerSettings } from '../AutoReviewerSettings';
import type { AutonomyConfig } from '@/lib/types';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';

const fetchToukaMock = vi.fn();
vi.mock('@/lib/connections', () => ({
  fetchTouka: (...args: unknown[]) => fetchToukaMock(...args),
  getActiveProxyUrl: () => 'http://proxy.test',
}));

// Radix Select 在 jsdom 難以驅動；換成原生 <select>（比照 IdentityMapping.test）。
vi.mock('@/components/ui/select', async () => {
  const { createElement } = await import('react');
  interface Opt { props: { value: string; children: string } }
  const Select = ({ value, onValueChange, children }: {
    value: string; onValueChange?: (v: string) => void; children: React.ReactNode;
  }) => {
    const opts: Opt[] = [];
    const walk = (node: unknown) => {
      if (Array.isArray(node)) return node.forEach(walk);
      const el = node as { props?: { children?: unknown; value?: string } } | null;
      if (!el || typeof el !== 'object' || !el.props) return;
      if (typeof el.props.value === 'string') opts.push(el as Opt);
      else walk(el.props.children);
    };
    walk(children);
    return createElement(
      'select',
      { 'aria-label': 'reviewer', value, onChange: (e: React.ChangeEvent<HTMLSelectElement>) => onValueChange?.(e.target.value) },
      opts.map(o => createElement('option', { key: o.props.value, value: o.props.value }, o.props.children)),
    );
  };
  const Pass = ({ children }: { children?: React.ReactNode }) => children as React.ReactElement;
  const Item = ({ value, children }: { value: string; children: string }) =>
    createElement('span', { 'data-value': value }, children);
  return { Select, SelectContent: Pass, SelectItem: Item, SelectTrigger: Pass, SelectValue: () => null };
});

const MODELS = [
  { id: 'claude-haiku-5-5', name: 'Claude Haiku 5.5', provider: 'anthropic' },
  { id: 'deepseek-chat', name: 'deepseek-chat', provider: 'deepseek' },
];

function config(model = ''): AutonomyConfig {
  return {
    react_loop: { max_tool_rounds: 0 },
    auto_review: { model, max_tokens: 512, timeout_s: 20, headless_terminal: false },
  } as unknown as AutonomyConfig;
}

function renderSettings(autonomyConfig: AutonomyConfig | null = config()) {
  const setAutonomyConfig = vi.fn();
  const saveAutonomy = vi.fn().mockResolvedValue(true);
  render(
    <AutoReviewerSettings
      autonomyConfig={autonomyConfig}
      setAutonomyConfig={setAutonomyConfig}
      isSavingAutonomy={false}
      saveAutonomy={saveAutonomy}
    />,
  );
  return { setAutonomyConfig, saveAutonomy };
}

const optionTexts = () =>
  Array.from((screen.getByLabelText('reviewer') as HTMLSelectElement).options).map(o => o.textContent);

beforeEach(() => {
  fetchToukaMock.mockReset();
  fetchToukaMock.mockResolvedValue({ ok: true, json: async () => ({ models: MODELS }) });
});

describe('AutoReviewerSettings', () => {
  it('lists only the key-configured models from the API and starts unset', async () => {
    renderSettings();
    await waitFor(() => expect(optionTexts()).toEqual(['未設定', 'Claude Haiku 5.5 (anthropic)', 'deepseek-chat (deepseek)']));
    expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/autonomy/reviewer-models');
    expect(screen.getByLabelText('reviewer')).toHaveValue('__unset__');
    // 建議值只是提示，不會被選上
    expect(screen.getByText('建議：Claude Haiku 5.5。不會自動幫你選。')).toBeInTheDocument();
  });

  it('shows the data, cost and unset notices', async () => {
    renderSettings();
    expect(screen.getByText('Auto 會把你最近 3 則訊息和要做的動作送到所選模型的廠商審查。')).toBeInTheDocument();
    expect(screen.getByText('審查費用算在被審的靈魂身上，用量統計裡另外標成審查員。')).toBeInTheDocument();
    expect(screen.getByText('沒設模型時，Auto 遇到要審的動作一律轉給你；你不在場就拒絕。')).toBeInTheDocument();
    await waitFor(() => expect(optionTexts()).toHaveLength(3));
  });

  it('writes the chosen model into auto_review and keeps the other fields', async () => {
    const { setAutonomyConfig } = renderSettings();
    await waitFor(() => expect(optionTexts()).toHaveLength(3));
    fireEvent.change(screen.getByLabelText('reviewer'), { target: { value: 'deepseek-chat' } });
    expect(setAutonomyConfig).toHaveBeenLastCalledWith(expect.objectContaining({
      react_loop: { max_tool_rounds: 0 },
      auto_review: { model: 'deepseek-chat', max_tokens: 512, timeout_s: 20, headless_terminal: false },
    }));
  });

  it('choosing 未設定 saves an empty model', async () => {
    const { setAutonomyConfig } = renderSettings(config('claude-haiku-5-5'));
    await waitFor(() => expect(screen.getByLabelText('reviewer')).toHaveValue('claude-haiku-5-5'));
    fireEvent.change(screen.getByLabelText('reviewer'), { target: { value: '__unset__' } });
    expect(setAutonomyConfig.mock.lastCall?.[0].auto_review.model).toBe('');
  });

  it('save posts the shared autonomy config', async () => {
    const current = config('claude-haiku-5-5');
    const { saveAutonomy } = renderSettings(current);
    fireEvent.click(screen.getByRole('button', { name: '儲存審查員設定' }));
    expect(saveAutonomy).toHaveBeenCalledWith(current);
    await waitFor(() => expect(optionTexts()).toHaveLength(3));
  });

  it('warns when the saved model has no configured key', async () => {
    renderSettings(config('gpt-5'));
    expect(await screen.findByText('目前設定的 gpt-5 不在已設定金鑰的模型裡，審查會失敗並轉給你。')).toBeInTheDocument();
    expect(optionTexts()).not.toContain('gpt-5');
  });

  it('says so when no model has a configured key', async () => {
    fetchToukaMock.mockResolvedValueOnce({ ok: true, json: async () => ({ models: [] }) });
    renderSettings();
    expect(await screen.findByText('還沒有已設定金鑰的模型，請先到「供應商金鑰」設定。')).toBeInTheDocument();
  });

  it('shows an error when the list cannot load', async () => {
    fetchToukaMock.mockResolvedValueOnce({ ok: false, json: async () => ({}) });
    renderSettings();
    expect(await screen.findByRole('alert')).toHaveTextContent('無法載入審查員模型清單。');
  });
});

// r55：審查員等待秒數（1～120）。只改 auto_review.timeout_s，其他欄位原樣；存檔路徑不變。
describe('AutoReviewerSettings 審查員等待秒數', () => {
  const timeoutInput = () => screen.getByLabelText('審查員等待秒數') as HTMLInputElement;
  const saveButton = () => screen.getByRole('button', { name: '儲存審查員設定' });
  const withTimeout = (timeout_s: number) => {
    const c = config('claude-haiku-5-5');
    return { ...c, auto_review: { ...c.auto_review!, timeout_s } } as AutonomyConfig;
  };

  it('shows the saved wait and explains that a timeout never approves', async () => {
    renderSettings(config('claude-haiku-5-5'));
    expect(timeoutInput()).toHaveValue(20);
    expect(timeoutInput()).toHaveAttribute('max', '120');
    expect(screen.getByText('超過這個秒數還沒回答，就轉給你；你不在場就拒絕。等太久不會自動放行。範圍 1～120。')).toBeInTheDocument();
    expect(saveButton()).toBeEnabled();
    await waitFor(() => expect(optionTexts()).toHaveLength(3));
  });

  it('changing the wait writes only auto_review.timeout_s', async () => {
    const { setAutonomyConfig } = renderSettings(config('claude-haiku-5-5'));
    fireEvent.change(timeoutInput(), { target: { value: '45' } });
    expect(setAutonomyConfig).toHaveBeenLastCalledWith({
      react_loop: { max_tool_rounds: 0 },
      auto_review: { model: 'claude-haiku-5-5', max_tokens: 512, timeout_s: 45, headless_terminal: false },
    });
    await waitFor(() => expect(optionTexts()).toHaveLength(3));
  });

  it.each([0, 121, Number.NaN])('an invalid wait (%s) disables save and shows a hint', async (value) => {
    const { saveAutonomy } = renderSettings(withTimeout(value));
    expect(saveButton()).toBeDisabled();
    expect(screen.getByText('請輸入 1～120 秒。')).toBeInTheDocument();
    fireEvent.click(saveButton());
    expect(saveAutonomy).not.toHaveBeenCalled();
    await waitFor(() => expect(optionTexts()).toHaveLength(3));
  });

  it('clearing the field leaves it blank and blocks save', async () => {
    const { setAutonomyConfig } = renderSettings(config('claude-haiku-5-5'));
    fireEvent.change(timeoutInput(), { target: { value: '' } });
    expect(setAutonomyConfig.mock.lastCall?.[0].auto_review.timeout_s).toBeNaN();
    await waitFor(() => expect(optionTexts()).toHaveLength(3));
  });

  it('a blank wait is shown as an empty field', async () => {
    renderSettings(withTimeout(Number.NaN));
    expect(timeoutInput().value).toBe('');
    await waitFor(() => expect(optionTexts()).toHaveLength(3));
  });

  it('is not editable without an auto_review block', async () => {
    renderSettings({ react_loop: { max_tool_rounds: 0 } } as unknown as AutonomyConfig);
    expect(timeoutInput()).toBeDisabled();
    expect(saveButton()).toBeDisabled();
    await waitFor(() => expect(optionTexts()).toHaveLength(3));
  });
});

describe('Auto reviewer data notice wording (④甲)', () => {
  const VENDORS = /anthropic|claude|openai|gpt|gemini|google|grok|xai|deepseek|haiku/i;

  it('zh-TW and en say the data goes to the selected model provider, naming no vendor', () => {
    expect(zhTW.settings.auto_reviewer.data_notice).toBe('Auto 會把你最近 3 則訊息和要做的動作送到所選模型的廠商審查。');
    expect(en.settings.auto_reviewer.data_notice).toBe(
      'Auto sends your last 3 messages and the pending action to the provider of the selected model for review.',
    );
    expect(zhTW.settings.auto_reviewer.data_notice).not.toMatch(VENDORS);
    expect(en.settings.auto_reviewer.data_notice).not.toMatch(VENDORS);
  });

  it('no longer mentions incognito chats: they always run in Manual (U2c-9)', () => {
    expect(zhTW.settings.auto_reviewer.data_notice).not.toMatch(/無痕/);
    expect(en.settings.auto_reviewer.data_notice).not.toMatch(/incognito/i);
  });
});
