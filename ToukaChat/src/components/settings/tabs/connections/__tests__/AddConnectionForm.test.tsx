/**
 * AddConnectionForm 編輯模式 —— 安全回歸測試（#3）。
 *
 * 資安要求：編輯既有連線時，**既有 API Key 絕不可回填到表單／出現在 DOM**。未勾「更換金鑰」
 * 時連金鑰輸入框都不渲染（保留現有），只有明確勾選才出現新金鑰輸入框。
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { AddConnectionForm } from '../AddConnectionForm';
import type { Connection } from '@/lib/types';

const SECRET = 'sk-super-secret-value';
const conn: Connection = {
  id: 'conn-x',
  name: 'My OpenAI',
  url: 'https://api.openai.com/v1',
  apiKey: SECRET,
  provider: 'openai',
  protocol: 'openai',
  enabled: true,
};

describe('AddConnectionForm 編輯模式（永不回填金鑰）', () => {
  it('既有金鑰不出現在任何輸入框，也不出現在畫面文字', () => {
    render(
      <AddConnectionForm onAdd={vi.fn()} editing={conn} onSave={vi.fn()} onCancelEdit={vi.fn()} />,
    );
    for (const el of Array.from(document.querySelectorAll('input'))) {
      expect((el as HTMLInputElement).value).not.toBe(SECRET);
    }
    expect(document.body.textContent ?? '').not.toContain(SECRET);
  });

  it('預設不渲染金鑰輸入框（保留現有），但帶入其餘欄位', () => {
    render(
      <AddConnectionForm onAdd={vi.fn()} editing={conn} onSave={vi.fn()} onCancelEdit={vi.fn()} />,
    );
    // 未勾「更換金鑰」→ 無 password 輸入框
    expect(document.querySelectorAll('input[type="password"]').length).toBe(0);
    // 非金鑰欄位有預填
    expect(screen.getByDisplayValue('My OpenAI')).toBeTruthy();
    expect(screen.getByDisplayValue('https://api.openai.com/v1')).toBeTruthy();
  });

  it('新增模式（無 editing）照常顯示金鑰輸入框', () => {
    render(<AddConnectionForm onAdd={vi.fn()} />);
    expect(document.querySelectorAll('input[type="password"]').length).toBe(1);
  });

  it('URL 欄位 placeholder 為所選供應商的預設 base_url（provider 感知，P4）', () => {
    render(<AddConnectionForm onAdd={vi.fn()} />);
    // 預設供應商 proxy → placeholder = PROVIDER_CATALOG.proxy.defaultBaseUrl
    expect(screen.getByPlaceholderText('http://localhost:8000')).toBeTruthy();
  });
});

describe('AddConnectionForm 逾時欄位（P5-#4）', () => {
  it('填逾時秒數 → onAdd 帶 requestTimeoutMs（秒×1000）', () => {
    const onAdd = vi.fn();
    render(<AddConnectionForm onAdd={onAdd} />);
    fireEvent.change(screen.getByPlaceholderText('http://localhost:8000'), { target: { value: 'http://x' } });
    fireEvent.change(screen.getByPlaceholderText('留空＝不限'), { target: { value: '30' } });
    fireEvent.click(screen.getByRole('button', { name: /新增連線/ }));
    expect(onAdd).toHaveBeenCalledTimes(1);
    expect(onAdd.mock.calls[0][0].requestTimeoutMs).toBe(30000);
  });

  it('逾時留空 → onAdd 帶 requestTimeoutMs undefined（不限）', () => {
    const onAdd = vi.fn();
    render(<AddConnectionForm onAdd={onAdd} />);
    fireEvent.change(screen.getByPlaceholderText('http://localhost:8000'), { target: { value: 'http://x' } });
    fireEvent.click(screen.getByRole('button', { name: /新增連線/ }));
    expect(onAdd).toHaveBeenCalledTimes(1);
    expect(onAdd.mock.calls[0][0].requestTimeoutMs).toBeUndefined();
  });
});

describe('AddConnectionForm 直連標示（P10）', () => {
  const NOTICE = '直連：從瀏覽器直接打模型，沒有工具、不受權限模式管、不記帳。';

  it('非 Touka Proxy 的連線標明直連沒有工具、不受權限模式管、不記帳', () => {
    render(<AddConnectionForm onAdd={vi.fn()} editing={conn} onSave={vi.fn()} onCancelEdit={vi.fn()} />);
    expect(screen.getByRole('note')).toHaveTextContent(NOTICE);
  });

  it('Touka Proxy 連線不顯示直連標示', () => {
    render(<AddConnectionForm onAdd={vi.fn()} />);
    expect(screen.queryByText(NOTICE)).toBeNull();
  });
});
