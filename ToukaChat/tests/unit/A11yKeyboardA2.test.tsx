/**
 * WCAG A2 批：鍵盤可操作性行為測試（2.1.1 / 4.1.2）。
 * - SessionItem 會話列：可聚焦、Enter/Space 切換、子元素事件不誤觸切換
 * - ToolStepTracker 展開列：button 語意＋aria-expanded
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SessionItem } from '@/components/sidebar/left/SessionItem';
import { ToolStepTracker } from '@/components/chat/ToolStepTracker';
import type { Session } from '@/lib/types';
import type { ToolStep } from '@/lib/tree_history';

const session = {
  id: 's1',
  title: 'KeyboardTest',
  updatedAt: 1760000000000,
  is_home: false,
} as unknown as Session;

function renderItem(overrides: Partial<React.ComponentProps<typeof SessionItem>> = {}) {
  const onSwitch = vi.fn();
  const onSaveTitle = vi.fn();
  const props: React.ComponentProps<typeof SessionItem> = {
    session,
    isActive: false,
    editingSessionId: null,
    editTitle: '',
    deleteConfirmId: null,
    projects: [],
    onSwitch,
    onStartEdit: vi.fn(),
    onSaveTitle,
    onEditTitleChange: vi.fn(),
    onDelete: vi.fn(),
    onClearDeleteConfirm: vi.fn(),
    onExport: vi.fn(),
    onSplitExport: vi.fn(),
    onSetHome: vi.fn(),
    onUnsetHome: vi.fn(),
    onSetProject: vi.fn(),
    ...overrides,
  };
  const utils = render(<SessionItem {...props} />);
  return { ...utils, onSwitch, onSaveTitle };
}

describe('SessionItem 鍵盤可操作性（WCAG 2.1.1 核心動線，A5 axe 後的無巢狀結構）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('切換會話是原生 <button>（鍵盤語意內建、無 nested-interactive）', () => {
    renderItem();
    const switchBtn = screen.getByRole('button', { name: /^KeyboardTest/ });
    expect(switchBtn.tagName).toBe('BUTTON');
    // axe nested-interactive 根治：外層容器不得再是 role="button"
    expect(switchBtn.closest('[role="button"]')).toBeNull();
  });

  it('啟動（click＝原生 Enter/Space 語意）切換會話', () => {
    const { onSwitch } = renderItem();
    fireEvent.click(screen.getByRole('button', { name: /^KeyboardTest/ }));
    expect(onSwitch).toHaveBeenCalledWith('s1');
  });

  it('當前會話以 aria-current 標記', () => {
    renderItem({ isActive: true });
    expect(screen.getByRole('button', { name: /^KeyboardTest/ })).toHaveAttribute(
      'aria-current',
      'true',
    );
  });

  it('選單觸發鈕有可及名稱（axe button-name 修正）', () => {
    renderItem();
    expect(
      screen.getByRole('button', { name: /會話選項：KeyboardTest/ }),
    ).toBeInTheDocument();
  });

  it('編輯模式下，標題輸入框按 Enter＝儲存標題、不誤觸會話切換', () => {
    const { onSwitch, onSaveTitle } = renderItem({
      editingSessionId: 's1',
      editTitle: 'New name',
    });
    const input = screen.getByDisplayValue('New name');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSaveTitle).toHaveBeenCalledWith('s1');
    expect(onSwitch).not.toHaveBeenCalled();
  });
});

describe('ToolStepTracker 展開列（WCAG 2.1.1 / 4.1.2）', () => {
  const step = {
    id: 't1',
    status: 'success',
    tool: 'bash',
    args: { cmd: 'ls' },
    result: 'file_a\nfile_b',
  } as unknown as ToolStep;

  it('可展開時為 button 語意，aria-expanded 跟隨開合', () => {
    render(<ToolStepTracker steps={[step]} />);
    const toggle = screen.getByRole('button', { name: /bash/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText(/file_a/)).toBeInTheDocument();
  });

  it('無結果（不可展開）時不提供假互動（disabled）', () => {
    const pending = { ...step, id: 't2', status: 'running', result: undefined } as unknown as ToolStep;
    render(<ToolStepTracker steps={[pending]} />);
    const toggle = screen.getByRole('button', { name: /bash/i });
    expect(toggle).toBeDisabled();
  });
});
