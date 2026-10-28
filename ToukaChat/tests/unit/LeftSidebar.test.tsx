import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import LeftSidebar from '@/components/LeftSidebar';

// Mock dependencies to avoid rendering collapse error
vi.mock('@/lib/SystemStatusContext', () => ({
  useSystemStatus: () => ({ status: { mode: 'hybrid' } })
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({ sessions: [] })
}));
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    activeSessionId: null,
    handleNewSession: vi.fn(),
    handleSwitchSession: vi.fn(),
    handleSetHome: vi.fn(),
    handleSetProject: vi.fn(),
    handleUnsetHome: vi.fn()
  })
}));
vi.mock('@/lib/providers/ProjectProvider', () => ({
  useProjects: () => ({ projects: [] })
}));
vi.mock('@/hooks/useSessionGroups', () => ({
  useSessionGroups: () => ({ sessionGroups: {} })
}));
vi.mock('@/hooks/useSessionActions', () => ({
  useSessionActions: () => ({
    editingSessionId: null,
    editTitle: '',
    deleteConfirmId: null,
    handleStartEdit: vi.fn(),
    handleSaveTitle: vi.fn(),
    setEditTitle: vi.fn(),
    handleDelete: vi.fn(),
    handleExport: vi.fn(),
    handleSplitExport: vi.fn()
  })
}));

vi.mock('@/components/sidebar/left/NavigationArea', () => ({
  NavigationArea: () => <div>Navigation Area</div>
}));

describe('LeftSidebar TDD-Red tests', () => {
  it('TDD-Red: does not read collapsed state from localStorage internally, only relies on isCollapsed prop', () => {
    // 當前 LeftSidebar 還在使用 useLocalStorage 內部維護狀態。
    // 在修復為 SSOT 後，LeftSidebar 應只根據 isCollapsed 渲染，內部不含 useLocalStorage。
    const { container } = render(<LeftSidebar isCollapsed={true} />);
    const outerDiv = container.firstChild as HTMLElement;
    expect(outerDiv?.classList.contains('w-0')).toBe(true);
  });
});

describe('LeftSidebar — 頂端的收合鈕（上導覽列拿掉後）', () => {
  it('有傳 onCollapse（桌面版）才有收合鈕，按了會呼叫', () => {
    const onCollapse = vi.fn();
    render(<LeftSidebar onCollapse={onCollapse} />);
    const button = screen.getByRole('button', { name: '收合側邊欄' });
    expect(button).toHaveAttribute('title', '收合側邊欄');
    fireEvent.click(button);
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it('沒傳 onCollapse（手機版 Sheet）就沒有收合鈕', () => {
    render(<LeftSidebar />);
    expect(screen.queryByRole('button', { name: '收合側邊欄' })).toBeNull();
  });

  // 桌面版的 page.tsx 會傳 takeFocusRequest：這次展開是按鈕要求的才回 true。
  const requested = () => true;
  const nextFrame = () => new Promise(resolve => requestAnimationFrame(() => resolve(null)));

  it('側欄剛展開、焦點掉在 body 時，等下一個畫面把焦點交給收合鈕', async () => {
    // 側欄的 visibility 有過場：剛換成展開的那一刻瀏覽器仍當它看不見，當下 focus() 會失效（jsdom 沒有 CSS 測不到），
    // 所以要等下一個畫面再交；這裡確認不是當下同步交。
    const { rerender } = render(<LeftSidebar isCollapsed={true} onCollapse={vi.fn()} takeFocusRequest={requested} />);
    (document.activeElement as HTMLElement | null)?.blur();
    rerender(<LeftSidebar isCollapsed={false} onCollapse={vi.fn()} takeFocusRequest={requested} />);
    const button = screen.getByRole('button', { name: '收合側邊欄' });
    expect(button).not.toHaveFocus();
    await waitFor(() => expect(button).toHaveFocus());
  });

  it('過場還沒走、這一畫面交不到焦點時，下一個畫面再試', async () => {
    // 真瀏覽器裡第一次 focus() 會觸發過場開始，當下仍是看不見；jsdom 沒有 CSS，用「第一次 focus 不生效」模擬。
    const realFocus = HTMLElement.prototype.focus;
    let calls = 0;
    const spy = vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function (this: HTMLElement, options?: FocusOptions) {
      if (this.getAttribute('aria-label') === '收合側邊欄' && ++calls === 1) return;
      realFocus.call(this, options);
    });
    try {
      const { rerender } = render(<LeftSidebar isCollapsed={true} onCollapse={vi.fn()} takeFocusRequest={requested} />);
      (document.activeElement as HTMLElement | null)?.blur();
      rerender(<LeftSidebar isCollapsed={false} onCollapse={vi.fn()} takeFocusRequest={requested} />);
      const button = screen.getByRole('button', { name: '收合側邊欄' });
      await waitFor(() => expect(button).toHaveFocus());
      expect(calls).toBe(2);
    } finally {
      spy.mockRestore();
    }
  });

  it('一直交不到焦點時，最多試 30 個畫面就停', () => {
    // 用假的畫面計時，不靠真實等待（整套平行跑、CPU 忙時才不會偶發失敗）；30 是 EXPAND_FOCUS_MAX_FRAMES。
    let calls = 0;
    const spy = vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function (this: HTMLElement) {
      if (this.getAttribute('aria-label') === '收合側邊欄') calls += 1;
    });
    const { rerender } = render(<LeftSidebar isCollapsed={true} onCollapse={vi.fn()} takeFocusRequest={requested} />);
    (document.activeElement as HTMLElement | null)?.blur();
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] });
    try {
      rerender(<LeftSidebar isCollapsed={false} onCollapse={vi.fn()} takeFocusRequest={requested} />);
      vi.advanceTimersByTime(16 * 100);
      expect(calls).toBe(30);
    } finally {
      vi.useRealTimers();
      spy.mockRestore();
    }
  });

  it('等下一個畫面時，焦點已經移到別處就不搶', async () => {
    const input = document.createElement('input');
    document.body.appendChild(input);
    try {
      const { rerender } = render(<LeftSidebar isCollapsed={true} onCollapse={vi.fn()} takeFocusRequest={requested} />);
      (document.activeElement as HTMLElement | null)?.blur();
      rerender(<LeftSidebar isCollapsed={false} onCollapse={vi.fn()} takeFocusRequest={requested} />);
      input.focus();
      await nextFrame();
      await nextFrame();
      expect(input).toHaveFocus();
    } finally {
      input.remove();
    }
  });

  it('這次展開不是按鈕要求的（面板掛上時自己同步狀態）就不搶焦點', async () => {
    const takeFocusRequest = vi.fn().mockReturnValue(false);
    const { rerender } = render(<LeftSidebar isCollapsed={true} onCollapse={vi.fn()} takeFocusRequest={takeFocusRequest} />);
    (document.activeElement as HTMLElement | null)?.blur();
    rerender(<LeftSidebar isCollapsed={false} onCollapse={vi.fn()} takeFocusRequest={takeFocusRequest} />);
    expect(takeFocusRequest).toHaveBeenCalledWith(true);
    await nextFrame();
    await nextFrame();
    expect(screen.getByRole('button', { name: '收合側邊欄' })).not.toHaveFocus();
  });

  it('沒傳 takeFocusRequest（手機版 Sheet）就不移焦點', async () => {
    const { rerender } = render(<LeftSidebar isCollapsed={true} onCollapse={vi.fn()} />);
    (document.activeElement as HTMLElement | null)?.blur();
    rerender(<LeftSidebar isCollapsed={false} onCollapse={vi.fn()} />);
    await nextFrame();
    await nextFrame();
    expect(screen.getByRole('button', { name: '收合側邊欄' })).not.toHaveFocus();
  });

  it('一開始就展開時不搶焦點', () => {
    (document.activeElement as HTMLElement | null)?.blur();
    render(<LeftSidebar isCollapsed={false} onCollapse={vi.fn()} takeFocusRequest={requested} />);
    expect(screen.getByRole('button', { name: '收合側邊欄' })).not.toHaveFocus();
  });
});
