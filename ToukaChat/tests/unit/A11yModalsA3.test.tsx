/**
 * WCAG A3 批：自製 Modal 遷移 Radix Dialog（2.1.2 焦點陷阱／Esc／焦點歸位）。
 * 行為驗證：開啟即有 dialog 語意、焦點落在 dialog 內、Esc 觸發關閉回呼。
 * （SoulStudio 體積大、相依多，由 tests/guard/a11y_basics.test.ts 的源碼掃描覆蓋。）
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import McpInstallModal from '@/components/modals/EvolutionModal';
import ImportAgentModal from '@/components/modals/ImportAgentModal';
import type { SkillInstallRequest } from '@/lib/types';

vi.mock('@/hooks/useEvolution', () => ({
  useEvolution: () => ({
    isScanning: false,
    scanResult: null,
    isInstalling: false,
    runScan: vi.fn(),
    confirmInstall: vi.fn(),
  }),
}));

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));

const installRequest = {
  id: 'req1',
  name: 'demo-skill',
  command: 'uvx',
  args: ['demo'],
  env_requirements: [],
} as unknown as SkillInstallRequest;

describe('McpInstallModal（EvolutionModal）Radix Dialog 遷移', () => {
  beforeEach(() => vi.clearAllMocks());

  it('開啟即有 dialog 語意且焦點被困在 dialog 內', () => {
    const onClose = vi.fn();
    render(<McpInstallModal installRequest={installRequest} onClose={onClose} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it('Esc 關閉（觸發 onClose）', () => {
    const onClose = vi.fn();
    render(<McpInstallModal installRequest={installRequest} onClose={onClose} />);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});

describe('ImportAgentModal Radix Dialog 遷移', () => {
  const props = {
    pendingSessions: [{ id: 's1', title: '未知對話' }],
    availableModels: [{ id: 'Alice', name: 'Alice' }],
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
  };

  beforeEach(() => vi.clearAllMocks());

  it('開啟即有 dialog 語意且焦點被困在 dialog 內', () => {
    render(<ImportAgentModal {...props} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it('Esc 關閉（觸發 onCancel）', () => {
    render(<ImportAgentModal {...props} />);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(props.onCancel).toHaveBeenCalled();
  });

  it('既有功能不變：選擇模型後確認匯入', () => {
    render(<ImportAgentModal {...props} />);
    fireEvent.click(screen.getByRole('button', { name: /確認匯入/ }));
    expect(props.onConfirm).toHaveBeenCalledWith('Alice');
  });
});
