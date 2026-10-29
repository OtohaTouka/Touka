/**
 * 對話訊息導覽列（展示元件）：每則一條、浮出預覽、點擊跳轉、目前那條、鍵盤 roving tabindex。
 * Enter／Space 是原生 button 行為，jsdom 測不到，改在真瀏覽器實測。
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import type { MessageNode } from '@/lib/tree_history';
import { buildNavEntries, groupNavEntries } from '@/lib/chat/messageNavigation';
import { MessageNavigator } from '../MessageNavigator';

function user(id: string, content: string, extra: Partial<MessageNode> = {}): MessageNode {
  return { id, parentId: null, childrenIds: [], role: 'user', content, ...extra };
}
function reply(id: string): MessageNode {
  return { id, parentId: null, childrenIds: [], role: 'assistant', content: 'ok' };
}

function ticksOf(linear: MessageNode[], max?: number) {
  return groupNavEntries(buildNavEntries(linear), max);
}

const THREE = ticksOf([user('u1', '第一句'), reply('a1'), user('u2', '第二句'), reply('a2'), user('u3', '第三句')]);

function renderNav(props: Partial<React.ComponentProps<typeof MessageNavigator>> = {}) {
  const onJump = vi.fn();
  const view = render(<MessageNavigator ticks={THREE} currentIndex={null} onJump={onJump} {...props} />);
  return { onJump, ...view };
}

function tabStops() {
  return screen.getAllByRole('button').filter(b => b.getAttribute('tabindex') === '0');
}

describe('MessageNavigator', () => {
  it('每則一條，名稱帶第幾則與預覽；外層是導覽、內層是直排工具列', () => {
    renderNav();
    expect(screen.getByRole('navigation', { name: '訊息導覽' })).toBeDefined();
    expect(screen.getByRole('toolbar').getAttribute('aria-orientation')).toBe('vertical');
    expect(screen.getAllByRole('button').map(b => b.getAttribute('aria-label'))).toEqual([
      '跳到第 1 則：第一句', '跳到第 2 則：第二句', '跳到第 3 則：第三句',
    ]);
    for (const button of screen.getAllByRole('button')) expect(button.getAttribute('type')).toBe('button');
  });

  it('分段的線寫出整段範圍', () => {
    const linear = ['a', 'b', 'c', 'd'].map(id => user(id, id));
    renderNav({ ticks: ticksOf(linear, 2) });
    expect(screen.getAllByRole('button').map(b => b.getAttribute('aria-label')))
      .toEqual(['跳到第 1～2 則：a', '跳到第 3～4 則：c']);
  });

  it('沒有文字時用附件或「沒有文字」當預覽', () => {
    renderNav({ ticks: ticksOf([
      user('u1', '', { attachment: { type: 'image', url: 'data:x' } }),
      user('u2', '', { attachment: { type: 'text', url: '', name: 'notes.md' } }),
      user('u3', '', { attachment: { type: 'text', url: '' } }),
      user('u4', '  '),
    ]) });
    expect(screen.getAllByRole('button').map(b => b.getAttribute('aria-label'))).toEqual([
      '跳到第 1 則：圖片', '跳到第 2 則：附件：notes.md', '跳到第 3 則：附件：檔案', '跳到第 4 則：（沒有文字）',
    ]);
  });

  it('滑鼠移上去浮出預覽，移開就收起', () => {
    renderNav();
    const second = screen.getByRole('button', { name: '跳到第 2 則：第二句' });
    expect(screen.queryByText('第二句')).toBeNull();
    fireEvent.mouseEnter(second);
    const preview = screen.getByText('第二句');
    expect(preview.closest('[aria-hidden="true"]')).not.toBeNull();
    fireEvent.mouseLeave(second);
    expect(screen.queryByText('第二句')).toBeNull();
  });

  it('按 Esc 收起滑鼠浮出的預覽，焦點不在導覽列也一樣', () => {
    renderNav();
    fireEvent.mouseEnter(screen.getByRole('button', { name: '跳到第 2 則：第二句' }));
    expect(screen.getByText('第二句')).toBeDefined();
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(screen.queryByText('第二句')).toBeNull();
  });

  it('按 Esc 收起鍵盤浮出的預覽，焦點留在原處', () => {
    renderNav();
    const [first, second] = screen.getAllByRole('button');
    act(() => first.focus());
    fireEvent.keyDown(first, { key: 'ArrowDown' });
    expect(screen.getByText('第二句')).toBeDefined();
    fireEvent.keyDown(second, { key: 'Escape' });
    expect(screen.queryByText('第二句')).toBeNull();
    expect(document.activeElement).toBe(second);
  });

  it('預覽開著時 Esc 只收預覽，不傳到輸入框；下一次 Esc 才給輸入框（生成中 Esc＝停止）', () => {
    const composerKeyDown = vi.fn();
    render(
      <>
        <textarea aria-label="composer" onKeyDown={composerKeyDown} />
        <MessageNavigator ticks={THREE} currentIndex={null} onJump={vi.fn()} />
      </>,
    );
    const composer = screen.getByRole('textbox', { name: 'composer' });
    act(() => composer.focus());
    fireEvent.mouseEnter(screen.getByRole('button', { name: '跳到第 2 則：第二句' }));
    fireEvent.keyDown(composer, { key: 'Escape' });
    expect(screen.queryByText('第二句')).toBeNull();
    expect(composerKeyDown).not.toHaveBeenCalled();
    fireEvent.keyDown(composer, { key: 'Escape' });
    expect(composerKeyDown).toHaveBeenCalledTimes(1);
  });

  it('失焦時收起預覽', () => {
    renderNav();
    const [first, second] = screen.getAllByRole('button');
    act(() => first.focus());
    fireEvent.keyDown(first, { key: 'ArrowDown' });
    expect(screen.getByText('第二句')).toBeDefined();
    act(() => second.blur());
    expect(screen.queryByText('第二句')).toBeNull();
  });

  it('點擊跳到那則並收起預覽', () => {
    const { onJump } = renderNav();
    const third = screen.getByRole('button', { name: '跳到第 3 則：第三句' });
    fireEvent.mouseEnter(third);
    fireEvent.click(third);
    expect(onJump).toHaveBeenCalledWith(4);
    expect(screen.queryByText('第三句')).toBeNull();
  });

  it('預覽是純文字，不解讀 HTML 或 Markdown', () => {
    const { container } = renderNav({ ticks: ticksOf([user('u1', '<b>x</b> **y**'), user('u2', 'z')]) });
    fireEvent.mouseEnter(screen.getAllByRole('button')[0]);
    expect(screen.getByText('<b>x</b> **y**')).toBeDefined();
    expect(container.querySelector('b, strong')).toBeNull();
  });

  it('目前那條標 aria-current，分段時落在段內也算', () => {
    const linear = ['a', 'b', 'c', 'd'].map(id => user(id, id));
    renderNav({ ticks: ticksOf(linear, 2), currentIndex: 3 });
    const [first, second] = screen.getAllByRole('button');
    expect(first.getAttribute('aria-current')).toBeNull();
    expect(second.getAttribute('aria-current')).toBe('true');
  });

  it('整排只有一個 Tab 停留點，預設在目前那條；沒有目前那條時在第一條', () => {
    const { rerender, onJump } = renderNav({ currentIndex: 2 });
    expect(tabStops().map(b => b.getAttribute('aria-label'))).toEqual(['跳到第 2 則：第二句']);
    rerender(<MessageNavigator ticks={THREE} currentIndex={null} onJump={onJump} />);
    expect(tabStops().map(b => b.getAttribute('aria-label'))).toEqual(['跳到第 1 則：第一句']);
  });

  it('上下鍵、Home、End 移動焦點與 Tab 停留點，並浮出預覽', () => {
    renderNav();
    const [first, second, third] = screen.getAllByRole('button');
    first.focus();
    fireEvent.keyDown(first, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(second);
    expect(tabStops()).toEqual([second]);
    expect(screen.getByText('第二句')).toBeDefined();
    fireEvent.keyDown(second, { key: 'End' });
    expect(document.activeElement).toBe(third);
    fireEvent.keyDown(third, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(third);
    fireEvent.keyDown(third, { key: 'Home' });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: 'ArrowUp' });
    expect(document.activeElement).toBe(first);
  });

  it('線變少之後仍恰好有一個 Tab 停留點', () => {
    const { rerender, onJump } = renderNav();
    const third = screen.getAllByRole('button')[2];
    act(() => third.focus());
    expect(tabStops()).toEqual([third]);
    rerender(<MessageNavigator ticks={THREE.slice(0, 2)} currentIndex={null} onJump={onJump} />);
    expect(tabStops()).toHaveLength(1);
  });
});
