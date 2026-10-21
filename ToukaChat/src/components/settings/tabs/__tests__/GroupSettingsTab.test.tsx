import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const saveWhitelist = vi.fn().mockResolvedValue(true);
const whitelist = { ToukaSoul: { 'old-alias': 'model-1' } };
const availableModels = [
  { id: 'Alice', owned_by: 'touka' },
  { id: 'ToukaGroup', owned_by: 'touka' },
  { id: 'my-custom', owned_by: 'custom' },
  { id: 'gemini-2.5-flash', owned_by: 'google' },
];

vi.mock('@/hooks/settings/useGroupSettings', () => ({
  useGroupSettings: () => ({
    whitelist,
    availableModels,
    isLoading: false,
    fetchWhitelist: vi.fn(),
    fetchModels: vi.fn(),
    saveWhitelist,
  }),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock('@/lib/error_handler', () => ({ errorMsg: () => 'err' }));

import { GroupSettingsTab } from '../GroupSettingsTab';

describe('GroupSettingsTab 別名 draft（P1）', () => {
  it('輸入新別名後不 blur 直接 Save → 送出新別名 key（不遺失）', async () => {
    render(<GroupSettingsTab />);
    const input = screen.getByDisplayValue('old-alias');
    fireEvent.change(input, { target: { value: 'new-alias' } });
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/ }));
    await waitFor(() => expect(saveWhitelist).toHaveBeenCalled());
    const payload = saveWhitelist.mock.calls[0][0];
    expect(payload.ToukaSoul['new-alias']).toBe('model-1');
    expect(payload.ToukaSoul['old-alias']).toBeUndefined();
    expect(Object.keys(payload)).toEqual(['ToukaSoul']);
  });
});

describe('GroupSettingsTab 群聊只限靈魂（ADR-0245）', () => {
  it('只有靈魂一區，新增選單只列靈魂', () => {
    render(<GroupSettingsTab />);
    expect(screen.queryByText('原生模型 (L0)')).toBeNull();
    expect(screen.queryByText('自定義模型 (L2)')).toBeNull();
    const select = screen.getByRole('combobox');
    const options = Array.from(select.querySelectorAll('option')).map((o) => o.value);
    expect(options).toEqual(['', 'Alice']);
  });
});

describe('GroupSettingsTab a11y（P3）', () => {
  it('加 model select 與別名 input 有可及名稱（aria-label）', () => {
    render(<GroupSettingsTab />);
    expect(screen.getAllByRole('combobox')[0].getAttribute('aria-label')).not.toBeNull();
    expect(screen.getByDisplayValue('old-alias').getAttribute('aria-label')).not.toBeNull();
  });
});
