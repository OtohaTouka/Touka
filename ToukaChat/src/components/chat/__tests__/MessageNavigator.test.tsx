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

  it('預覽那條因重新分段消失後，Esc 照常傳到輸入框', () => {
    const composerKeyDown = vi.fn();
    const view = (ticks: typeof THREE) => (
      <>
        <textarea aria-label="composer" onKeyDown={composerKeyDown} />
        <MessageNavigator ticks={ticks} currentIndex={null} onJump={vi.fn()} />
      </>
    );
    const { rerender } = render(view(THREE));
    const composer = screen.getByRole('textbox', { name: 'composer' });
    fireEvent.mouseEnter(screen.getByRole('button', { name: '跳到第 2 則：第二句' }));
    expect(screen.getByText('第二句')).toBeDefined();
    rerender(view(ticksOf([user('x1', '甲'), user('x2', '乙')])));
    expect(screen.queryByText('第二句')).toBeNull();
    fireEvent.keyDown(composer, { key: 'Escape' });
    expect(composerKeyDown).toHaveBeenCalledTimes(1);
  });

  it('預覽那條消失後又回來（分段變回原狀），預覽不會自己再出現', () => {
    const { rerender } = renderNav();
    fireEvent.mouseEnter(screen.getByRole('button', { name: '跳到第 2 則：第二句' }));
    expect(screen.getByText('第二句')).toBeDefined();
    rerender(<MessageNavigator ticks={ticksOf([user('x1', '甲'), user('x2', '乙')])} currentIndex={null} onJump={vi.fn()} />);
    rerender(<MessageNavigator ticks={THREE} currentIndex={null} onJump={vi.fn()} />);
    expect(screen.queryByText('第二句')).toBeNull();
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

describe('MessageNavigator 重新分段時的焦點交接', () => {
  const FOUR = ['a', 'b', 'c', 'd'].map(id => user(id, id));
  const nav = (ticks: typeof THREE, currentIndex: number | null = null) => (
    <MessageNavigator ticks={ticks} currentIndex={currentIndex} onJump={vi.fn()} />
  );
  const button = (name: string) => screen.getByRole('button', { name });

  // 修正前會失敗：d 的按鈕被移除，jsdom 把焦點清回 body。
  // 聚焦 d（位置 3）：涵蓋它的是 c，不是預設的 Tab 停留點 a，
  // 擋「永遠移到 Tab 停留點」與「focus() 沒經 onFocus 更新停留點」的寫法
  it('焦點那條因重新分段消失時，焦點移到涵蓋原位置的那條、不捲動，且它是唯一的 Tab 停留點', () => {
    const { rerender } = render(nav(ticksOf(FOUR)));
    act(() => button('跳到第 4 則：d').focus());
    const focusSpy = vi.spyOn(HTMLElement.prototype, 'focus');
    try {
      rerender(nav(ticksOf(FOUR, 2)));
      const covering = button('跳到第 3～4 則：c');
      expect(document.activeElement).toBe(covering);
      expect(tabStops()).toEqual([covering]);
      expect(focusSpy.mock.calls.some(([options]) => options?.preventScroll === true)).toBe(true);
    } finally {
      focusSpy.mockRestore();
    }
  });

  // 修正前會失敗：焦點掉到 body。目前那條指向第二條，擋「永遠移到第一條」的寫法
  it('原位置在新的線裡找不到時，焦點移到新的 Tab 停留點（目前那條）', () => {
    const { rerender } = render(nav(THREE));
    act(() => button('跳到第 3 則：第三句').focus()); // 位置 4
    rerender(nav(ticksOf([user('y1', '甲'), user('y2', '乙'), user('y3', '丙')]), 1)); // 位置 0～2
    const second = button('跳到第 2 則：乙');
    expect(document.activeElement).toBe(second);
    expect(tabStops()).toEqual([second]);
  });

  // 防退步
  it('焦點在導覽列外的元素時重新分段，焦點留在那個元素', () => {
    const view = (ticks: typeof THREE) => (
      <>
        <input aria-label="outside" />
        {nav(ticks)}
      </>
    );
    const { rerender } = render(view(ticksOf(FOUR)));
    const outside = screen.getByRole('textbox', { name: 'outside' });
    act(() => outside.focus());
    rerender(view(ticksOf(FOUR, 2)));
    expect(document.activeElement).toBe(outside);
  });

  // 防退步：擋拿掉「activeElement 不是 body 也不是 null 就不移」的條件。
  // React 在 layout 階段處理 autoFocus；輸入框排在導覽列前面，它先拿到焦點，導覽列的 layout effect 才跑。
  // late 為 false 時 `false` 佔住第一個位置，導覽列兩次 render 都是第二個子節點，不會重新掛載
  it('焦點那條消失的同一次重繪裡別的元素已拿到焦點，焦點留在那個元素、不被導覽列搶回', () => {
    const view = (ticks: typeof THREE, late: boolean) => (
      <>
        {late && <input aria-label="late" autoFocus />}
        {nav(ticks)}
      </>
    );
    const { rerender } = render(view(ticksOf(FOUR), false));
    act(() => button('跳到第 2 則：b').focus());
    rerender(view(ticksOf(FOUR, 2), true));
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'late' }));
  });

  // 防退步
  it('焦點那條在重新分段後還在時，焦點不動', () => {
    const { rerender } = render(nav(ticksOf(FOUR)));
    const first = button('跳到第 1 則：a');
    act(() => first.focus());
    rerender(nav(ticksOf(FOUR, 2)));
    expect(document.activeElement).toBe(first);
    expect(first.getAttribute('aria-label')).toBe('跳到第 1～2 則：a');
  });

  // 防退步
  it('聚焦後離開到 body，再重新分段使那條消失，焦點仍在 body', () => {
    const { rerender } = render(nav(ticksOf(FOUR)));
    const second = button('跳到第 2 則：b');
    act(() => second.focus());
    act(() => second.blur());
    expect(document.activeElement).toBe(document.body);
    rerender(nav(ticksOf(FOUR, 2)));
    expect(document.activeElement).toBe(document.body);
  });

  // 防退步：擋「記錄沒清掉」；重繪必須傳同一個 ticks 陣列參考
  it('聚焦後用相同的 ticks 重繪、再離開到 body，之後重新分段，焦點仍在 body', () => {
    const ticks = ticksOf(FOUR);
    const { rerender } = render(nav(ticks));
    const second = button('跳到第 2 則：b');
    act(() => second.focus());
    rerender(nav(ticks));
    act(() => second.blur());
    expect(document.activeElement).toBe(document.body);
    rerender(nav(ticksOf(FOUR, 2)));
    expect(document.activeElement).toBe(document.body);
  });
});
