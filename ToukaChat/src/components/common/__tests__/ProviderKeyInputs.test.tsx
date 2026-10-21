/**
 * ProviderKeyInputs —— 共用 provider 金鑰輸入元件（#4 批次 1）。
 *
 * 安全：只吃 boolean 狀態與「新值」輸入；輸入框一律留空，既有金鑰永不外露。
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { ProviderKeyInputs } from '../ProviderKeyInputs';
import { PROVIDER_KEYS } from '@/lib/providerKeys';

const setStatuses = {
  ANTHROPIC_API_KEY: true,
  OPENAI_API_KEY: false,
  GEMINI_API_KEY: false,
  GROK_API_KEY: false,
  TAVILY_API_KEY: false,
};

describe('ProviderKeyInputs 取得金鑰連結（P2）', () => {
  it('每個供應商渲染指向其控制台的連結，且 target/rel 安全', () => {
    render(<ProviderKeyInputs statuses={setStatuses} values={{}} onChange={vi.fn()} />);
    const links = screen.getAllByRole('link', { name: /取得金鑰/ });
    expect(links).toHaveLength(PROVIDER_KEYS.length);
    for (const a of links) {
      expect(a.getAttribute('target')).toBe('_blank');
      expect(a.getAttribute('rel')).toBe('noopener noreferrer');
    }
    const anthropic = PROVIDER_KEYS.find(p => p.id === 'anthropic');
    expect(links.some(a => a.getAttribute('href') === anthropic?.helpUrl)).toBe(true);
  });
});

describe('ProviderKeyInputs 清除鈕（P2）', () => {
  it('提供 onClear 時，僅「已設定」的金鑰顯示清除鈕', () => {
    render(<ProviderKeyInputs statuses={setStatuses} values={{}} onChange={vi.fn()} onClear={vi.fn()} />);
    expect(screen.getAllByRole('button', { name: '清除' })).toHaveLength(1);
  });

  it('未提供 onClear（首次精靈）→ 不顯示任何清除鈕', () => {
    render(<ProviderKeyInputs statuses={setStatuses} values={{}} onChange={vi.fn()} />);
    expect(screen.queryByRole('button', { name: '清除' })).toBeNull();
  });
});

describe('ProviderKeyInputs（共用：狀態＋留空輸入）', () => {
  it('每個 provider 一個留空的 password 輸入（已設定者亦留空）', () => {
    render(<ProviderKeyInputs statuses={{ ANTHROPIC_API_KEY: true }} values={{}} onChange={vi.fn()} />);
    const pwds = document.querySelectorAll('input[type="password"]');
    expect(pwds.length).toBe(PROVIDER_KEYS.length);
    for (const el of Array.from(pwds)) {
      expect((el as HTMLInputElement).value).toBe('');
    }
  });

  it('onChange 帶 env 名與新值', () => {
    const onChange = vi.fn();
    render(<ProviderKeyInputs statuses={{}} values={{}} onChange={onChange} />);
    const pwds = document.querySelectorAll('input[type="password"]');
    fireEvent.change(pwds[0], { target: { value: 'sk-x' } });
    expect(onChange).toHaveBeenCalledWith('ANTHROPIC_API_KEY', 'sk-x');
  });
});
