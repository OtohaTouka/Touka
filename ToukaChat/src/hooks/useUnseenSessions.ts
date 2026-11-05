import { useCallback, useMemo, useState } from 'react';
import { useLocalStorage } from './useLocalStorage';

const UNSEEN_KEY_PREFIX = 'touka_session_unseen';
// 瀏覽器裡的值可能被改壞：不是陣列就當成空的
const asIds = (value: unknown): string[] => Array.isArray(value) ? value : [];

/**
 * 「您沒在看時完成的」小點。鍵名含使用者 ID，只存對話編號；無痕只記在記憶體（ADR-0086）。
 * `useLocalStorage` 換鍵不重讀，所以呼叫端要用身分當 React key 重掛。
 */
export function useUnseenSessions(userId: string | undefined) {
  const [stored, setStored] = useLocalStorage<string[]>(`${UNSEEN_KEY_PREFIX}:${userId ?? 'anonymous'}`, []);
  const [unsaved, setUnsaved] = useState<string[]>([]);

  const markUnseen = useCallback((id: string, persist: boolean) => {
    const add = (current: string[]) => {
      const ids = asIds(current);
      return ids.includes(id) ? ids : [...ids, id];
    };
    if (persist) setStored(add);
    else setUnsaved(add);
  }, [setStored]);

  const clearUnseen = useCallback((id: string) => {
    const remove = (current: string[]) => {
      const ids = asIds(current);
      return ids.includes(id) ? ids.filter(item => item !== id) : ids;
    };
    setStored(remove);
    setUnsaved(remove);
  }, [setStored]);

  /** 留下還用得到的：對話被刪了、或再也下載不到，就不該留著編號。 */
  const pruneUnseen = useCallback((keep: (id: string) => boolean) => {
    const prune = (current: string[]) => {
      const ids = asIds(current);
      return ids.every(keep) ? ids : ids.filter(keep);
    };
    setStored(prune);
    setUnsaved(prune);
  }, [setStored]);

  const unseenIds = useMemo(() => new Set([...asIds(stored), ...unsaved]), [stored, unsaved]);

  return { unseenIds, markUnseen, clearUnseen, pruneUnseen };
}
