import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const h = vi.hoisted(() => ({ isLoading: true }));

vi.mock('@/hooks/settings/useGroupSettings', () => ({
  useGroupSettings: () => ({
    whitelist: { ToukaSoul: {} },
    availableModels: [],
    isLoading: h.isLoading,
    fetchWhitelist: vi.fn(),
    fetchModels: vi.fn(),
    saveWhitelist: vi.fn(),
  }),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock('@/lib/error_handler', () => ({ errorMsg: () => 'err' }));

import { GroupSettingsTab } from '../GroupSettingsTab';

describe('GroupSettingsTab A2：載入 spinner', () => {
  it('isLoading 時顯示 status spinner（aria-busy）', () => {
    h.isLoading = true;
    render(<GroupSettingsTab />);
    const status = screen.getByRole('status');
    expect(status.getAttribute('aria-busy')).toBe('true');
    expect(status.getAttribute('aria-label')).toBe('載入群聊設定中…');
  });

  it('載入完成後隱藏 spinner、顯示分類區塊', () => {
    h.isLoading = false;
    render(<GroupSettingsTab />);
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByText('靈魂代理人 (L1)')).toBeTruthy();
  });
});
