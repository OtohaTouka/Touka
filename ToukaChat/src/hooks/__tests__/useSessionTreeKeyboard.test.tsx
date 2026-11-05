import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import { createPortal } from 'react-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useSessionTreeKeyboard, type TreeRow } from '../useSessionTreeKeyboard';

const ROWS: TreeRow[] = [
  { id: 'folder-a', parentId: null, expanded: true },
  { id: 'a1', parentId: 'folder-a' },
  { id: 'a2', parentId: 'folder-a' },
  { id: 'folder-b', parentId: null, expanded: false },
  { id: 'other', parentId: null },
];

const handlers = { onToggle: vi.fn(), onActivate: vi.fn() };

function Tree({ rows = ROWS, preferredId = null }: { rows?: TreeRow[]; preferredId?: string | null }) {
  const { rootRef, rootProps, currentId } = useSessionTreeKeyboard({ rows, preferredId, ...handlers });
  return (
    <div ref={rootRef} role="tree" {...rootProps}>
      {rows.map(row => (
        <div key={row.id} role="treeitem" aria-selected={false} data-tree-row={row.id} data-testid={row.id} tabIndex={row.id === currentId ? 0 : -1}>
          {row.id === 'a1' && <input aria-label="rename" />}
          {row.id === 'a2' && <button>menu</button>}
          {row.id === 'other' && createPortal(<button>portal</button>, document.body)}
        </div>
      ))}
    </div>
  );
}

const row = (id: string) => screen.getByTestId(id);
const tabbable = () => screen.getAllByRole('treeitem').filter(item => item.tabIndex === 0).map(item => item.dataset.treeRow);

function pressOn(id: string, key: string) {
  row(id).focus();
  fireEvent.keyDown(document.activeElement as Element, { key });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('useSessionTreeKeyboard', () => {
  it('整個清單只有一列在 Tab 順序裡，預設第一列', () => {
    render(<Tree />);
    expect(tabbable()).toEqual(['folder-a']);
  });

  it('Tab 進來先到開著的對話；開著的不在清單上就第一列', () => {
    const { rerender } = render(<Tree preferredId="a2" />);
    expect(tabbable()).toEqual(['a2']);
    rerender(<Tree preferredId="gone" />);
    expect(tabbable()).toEqual(['folder-a']);
  });

  it('上下鍵移動，到頭尾就停，Tab 站跟著走', () => {
    render(<Tree />);
    pressOn('folder-a', 'ArrowDown');
    expect(row('a1')).toHaveFocus();
    expect(tabbable()).toEqual(['a1']);
    pressOn('folder-a', 'ArrowUp');
    expect(row('folder-a')).toHaveFocus();
    pressOn('other', 'ArrowDown');
    expect(row('other')).toHaveFocus();
  });

  it('Home、End 到第一列與最後一列', () => {
    render(<Tree />);
    pressOn('a1', 'End');
    expect(row('other')).toHaveFocus();
    pressOn('a1', 'Home');
    expect(row('folder-a')).toHaveFocus();
  });

  it('右鍵：收合的展開；展開的到第一個子對話；一般列不動', () => {
    render(<Tree />);
    pressOn('folder-b', 'ArrowRight');
    expect(handlers.onToggle).toHaveBeenCalledWith(ROWS[3]);
    expect(row('folder-b')).toHaveFocus();

    pressOn('folder-a', 'ArrowRight');
    expect(row('a1')).toHaveFocus();

    handlers.onToggle.mockClear();
    pressOn('other', 'ArrowRight');
    expect(handlers.onToggle).not.toHaveBeenCalled();
    expect(row('other')).toHaveFocus();
  });

  it('展開但底下沒有列時，右鍵不跑到下一個資料夾', () => {
    const rows: TreeRow[] = [{ id: 'empty', parentId: null, expanded: true }, { id: 'next', parentId: null, expanded: true }];
    const { unmount } = render(<Tree rows={rows} />);
    row('empty').focus();
    fireEvent.keyDown(row('empty'), { key: 'ArrowRight' });
    expect(row('empty')).toHaveFocus();
    unmount();
  });

  it('左鍵：展開的收合；子對話回到資料夾；最上層不動', () => {
    render(<Tree />);
    pressOn('folder-a', 'ArrowLeft');
    expect(handlers.onToggle).toHaveBeenCalledWith(ROWS[0]);

    pressOn('a2', 'ArrowLeft');
    expect(row('folder-a')).toHaveFocus();

    handlers.onToggle.mockClear();
    pressOn('other', 'ArrowLeft');
    expect(handlers.onToggle).not.toHaveBeenCalled();
    expect(row('other')).toHaveFocus();
  });

  it.each(['Enter', ' '])('按「%s」開那一列', (key) => {
    render(<Tree />);
    pressOn('a2', key);
    expect(handlers.onActivate).toHaveBeenCalledWith(ROWS[2]);
  });

  it('改名輸入框裡的方向鍵、Enter、空白不動清單', () => {
    render(<Tree />);
    const input = screen.getByRole('textbox', { name: 'rename' });
    input.focus();
    for (const key of ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' ']) {
      expect(fireEvent.keyDown(input, { key })).toBe(true);
      expect(input).toHaveFocus();
    }
    expect(handlers.onActivate).not.toHaveBeenCalled();
    expect(handlers.onToggle).not.toHaveBeenCalled();
  });

  it('焦點在「⋯」上按 Enter 是按鈕自己的，不開那一列；方向鍵照樣移動', () => {
    render(<Tree />);
    const menu = screen.getByRole('button', { name: 'menu' });
    menu.focus();
    expect(fireEvent.keyDown(menu, { key: 'Enter' })).toBe(true);
    expect(handlers.onActivate).not.toHaveBeenCalled();
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(row('folder-b')).toHaveFocus();
  });

  it('經 portal 畫在外面的選單，按鍵不動清單', () => {
    render(<Tree />);
    const portal = screen.getByRole('button', { name: 'portal' });
    portal.focus();
    expect(fireEvent.keyDown(portal, { key: 'ArrowUp' })).toBe(true);
    expect(portal).toHaveFocus();
  });

  it.each(['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' '])('清單處理的「%s」不讓瀏覽器再處理（頁面不捲動）', (key) => {
    render(<Tree />);
    row('a1').focus();
    expect(fireEvent.keyDown(row('a1'), { key })).toBe(false);
  });

  it('其他按鍵不攔', () => {
    render(<Tree />);
    row('a1').focus();
    expect(fireEvent.keyDown(row('a1'), { key: 'a' })).toBe(true);
    expect(fireEvent.keyDown(row('a1'), { key: 'Tab' })).toBe(true);
  });

  it('滑鼠點到別列，Tab 站跟過去', () => {
    render(<Tree />);
    act(() => row('other').focus());
    expect(tabbable()).toEqual(['other']);
  });

  it('焦點所在的列消失時，交給同一個位置的列', () => {
    const { rerender } = render(<Tree />);
    row('a2').focus();
    rerender(<Tree rows={ROWS.filter(item => item.id !== 'a2')} />);
    expect(row('folder-b')).toHaveFocus();
    expect(tabbable()).toEqual(['folder-b']);
  });

  it('消失的是最後一列時，交給新的最後一列', () => {
    const { rerender } = render(<Tree />);
    row('other').focus();
    rerender(<Tree rows={ROWS.filter(item => item.id !== 'other')} />);
    expect(row('folder-b')).toHaveFocus();
  });

  it('列消失時焦點已經在別處，就不搶', () => {
    const outside = document.createElement('input');
    document.body.appendChild(outside);
    try {
      const { rerender } = render(<Tree />);
      row('a2').focus();
      outside.focus();
      rerender(<Tree rows={ROWS.filter(item => item.id !== 'a2')} />);
      expect(outside).toHaveFocus();
    } finally {
      outside.remove();
    }
  });
});
