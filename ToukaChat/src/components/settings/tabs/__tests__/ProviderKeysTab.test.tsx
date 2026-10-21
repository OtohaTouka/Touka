/**
 * ProviderKeysTab —— 安全回歸測試（#1）。
 *
 * 資安要求：分頁**永遠拿不到也不顯示既有金鑰**（hook 只暴露 set/unset boolean），輸入框一律
 * 留空；儲存只送有輸入的鍵。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { ProviderKeysTab } from '../ProviderKeysTab';
import { errorMsg } from '@/lib/error_handler';
import { PROVIDER_KEYS } from '@/lib/providerKeys';

const mockSaveKeys = vi.fn().mockResolvedValue(true);
const mockClearKey = vi.fn().mockResolvedValue(true);
const mockHook = {
  // 即使某些已設定（true），也只有 boolean——無真值可洩漏
  statuses: {
    ANTHROPIC_API_KEY: true,
    OPENAI_API_KEY: false,
    GEMINI_API_KEY: false,
    GROK_API_KEY: false,
    TAVILY_API_KEY: false,
  },
  loading: false,
  saving: false,
  fetchStatuses: vi.fn(),
  saveKeys: mockSaveKeys,
  clearKey: mockClearKey,
};

vi.mock('@/hooks/settings/useProviderKeySettings', () => ({
  useProviderKeySettings: () => mockHook,
}));

// 清除金鑰走確認框：此處自動確認。
vi.mock('@/hooks/useConfirmToast', () => ({
  useConfirmToast: () => ({
    confirmToast: (_m: string, onConfirm: () => void) => { onConfirm(); },
  }),
}));

describe('ProviderKeysTab（金鑰永不外露於表單）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('所有金鑰輸入框預設一律留空（已設定者亦然）', () => {
    render(<ProviderKeysTab />);
    const pwds = document.querySelectorAll('input[type="password"]');
    expect(pwds.length).toBe(PROVIDER_KEYS.length);
    for (const el of Array.from(pwds)) {
      expect((el as HTMLInputElement).value).toBe('');
    }
  });

  it('儲存只送有輸入的鍵', () => {
    render(<ProviderKeysTab />);
    const pwds = document.querySelectorAll('input[type="password"]');
    // 第一個是 anthropic（PROVIDER_KEYS 順序）
    fireEvent.change(pwds[0], { target: { value: 'sk-new-anthropic' } });
    fireEvent.click(screen.getByRole('button', { name: /儲存金鑰/ }));
    expect(mockSaveKeys).toHaveBeenCalledWith({ ANTHROPIC_API_KEY: 'sk-new-anthropic' });
  });

  it('儲存失敗時顯示錯誤訊息（不再靜默）', async () => {
    mockSaveKeys.mockResolvedValueOnce(false);
    render(<ProviderKeysTab />);
    const pwds = document.querySelectorAll('input[type="password"]');
    fireEvent.change(pwds[0], { target: { value: 'sk-bad' } });
    fireEvent.click(screen.getByRole('button', { name: /儲存金鑰/ }));
    expect(await screen.findByText(errorMsg('CONFIG_SAVE_FAILED'))).toBeDefined();
  });

  it('已設定的金鑰才顯示「清除」鈕，點擊（確認後）呼叫 clearKey', () => {
    render(<ProviderKeysTab />);
    // 只有 ANTHROPIC_API_KEY 為 true → 恰好一個清除鈕
    const clearBtns = screen.getAllByRole('button', { name: '清除' });
    expect(clearBtns).toHaveLength(1);
    fireEvent.click(clearBtns[0]);
    expect(mockClearKey).toHaveBeenCalledWith('ANTHROPIC_API_KEY');
  });
});
