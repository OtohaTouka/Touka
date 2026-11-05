import { useCallback, useState } from 'react';
import { useLocalStorage } from './useLocalStorage';

const COLLAPSED_KEY_PREFIX = 'touka_session_tree_collapsed';

type Flags = Record<string, boolean>;

function withFlag(flags: Flags, key: string, value: boolean): Flags {
  const next = { ...flags };
  if (value) next[key] = true;
  else delete next[key];
  return next;
}

/**
 * 對話清單的收合狀態。鍵名含使用者 ID，各帳號分開記。
 * `useLocalStorage` 換鍵不重讀，所以呼叫端要用身分當 React key 重掛（`SessionTree`）。
 */
export function useSessionTreeState(userId: string | undefined) {
  const [stored, setStored] = useLocalStorage<Flags | null>(`${COLLAPSED_KEY_PREFIX}:${userId ?? 'anonymous'}`, {});
  // 只有無痕對話的資料夾與專案分組：只記在記憶體，不寫進瀏覽器（ADR-0086）
  const [unsaved, setUnsaved] = useState<Flags>({});
  const [expandedMore, setExpandedMore] = useState<Flags>({});

  const isCollapsed = useCallback((key: string) => !!(unsaved[key] ?? stored?.[key]), [stored, unsaved]);

  const setCollapsed = useCallback((key: string, collapsed: boolean, persist: boolean) => {
    if (persist) {
      setUnsaved(current => withFlag(current, key, false));
      setStored(current => withFlag(current ?? {}, key, collapsed));
    } else {
      setUnsaved(current => withFlag(current, key, collapsed));
    }
  }, [setStored]);

  const isShowingAll = useCallback((key: string) => !!expandedMore[key], [expandedMore]);
  const showAll = useCallback((key: string) => setExpandedMore(current => withFlag(current, key, true)), []);

  return { isCollapsed, setCollapsed, isShowingAll, showAll };
}
