import React, { useCallback, useEffect, useRef, useState } from 'react';

export interface TreeRow {
  id: string;
  parentId: string | null;
  /** 資料夾才有；收合是 false。 */
  expanded?: boolean;
}

interface UseSessionTreeKeyboardOptions {
  rows: TreeRow[];
  /** Tab 進清單時先到哪一列：開著的對話；不在清單上就第一列。 */
  preferredId: string | null;
  onToggle: (row: TreeRow) => void;
  onActivate: (row: TreeRow) => void;
}

/**
 * WAI-ARIA APG Tree View：整個清單只算一個 Tab 站，進來後用方向鍵（roving tabindex）。
 * 事件掛在清單根節點，每列只要帶 `data-tree-row`。
 */
export function useSessionTreeKeyboard({ rows, preferredId, onToggle, onActivate }: UseSessionTreeKeyboardOptions) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);

  const currentId = rows.some(row => row.id === focusedId) ? focusedId
    : rows.some(row => row.id === preferredId) ? preferredId : rows[0]?.id ?? null;

  const focusRow = useCallback((id: string) => {
    const elements = rootRef.current?.querySelectorAll<HTMLElement>('[data-tree-row]') ?? [];
    Array.from(elements).find(element => element.dataset.treeRow === id)?.focus();
  }, []);

  // 焦點所在的列消失了（按了「還有 N 個」、刪掉對話）：交給同一個位置的列，焦點不掉到頁面最前面
  const previousRows = useRef(rows);
  useEffect(() => {
    const lostIndex = previousRows.current.findIndex(row => row.id === focusedId);
    previousRows.current = rows;
    if (focusedId === null || lostIndex < 0 || rows.some(row => row.id === focusedId)) return;
    const next = rows[Math.min(lostIndex, rows.length - 1)];
    const active = document.activeElement;
    if (next && (!active || active === document.body)) focusRow(next.id);
  }, [rows, focusedId, focusRow]);

  const onFocus = useCallback((event: React.FocusEvent<HTMLElement>) => {
    const id = (event.target as HTMLElement).closest<HTMLElement>('[data-tree-row]')?.dataset.treeRow;
    if (id) setFocusedId(id);
  }, []);

  const onKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    // Alt+左鍵是瀏覽器上一頁；帶這些修飾鍵的都不是清單的按鍵
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target as HTMLElement;
    // 改名輸入框的方向鍵是移動游標
    if (target.tagName === 'INPUT') return;
    // 「⋯」選單經 portal 畫在外面，React 事件還是會冒上來；它不在任何一列裡，下面就不處理
    const rowElement = target.closest<HTMLElement>('[data-tree-row]');
    const index = rows.findIndex(row => row.id === rowElement?.dataset.treeRow);
    if (index < 0) return;
    const row = rows[index];
    const move = (next?: TreeRow) => { if (next) focusRow(next.id); };

    switch (event.key) {
      case 'ArrowDown': move(rows[index + 1]); break;
      case 'ArrowUp': move(rows[index - 1]); break;
      case 'Home': move(rows[0]); break;
      case 'End': move(rows[rows.length - 1]); break;
      case 'ArrowRight':
        if (row.expanded === false) onToggle(row);
        else if (row.expanded && rows[index + 1]?.parentId === row.id) move(rows[index + 1]);
        break;
      case 'ArrowLeft':
        if (row.expanded) onToggle(row);
        else move(rows.find(candidate => candidate.id === row.parentId));
        break;
      case 'Enter':
      case ' ':
        // 焦點在「⋯」上時是那顆按鈕自己的 Enter
        if (target !== rowElement) return;
        onActivate(row);
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  return { rootRef, currentId, rootProps: { onKeyDown, onFocus } };
}
