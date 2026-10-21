import { useCallback, useSyncExternalStore } from "react";

export function useMediaQuery(query: string) {
  // useSyncExternalStore：訂閱 MediaQueryList 的 'change'，query 變更時自動重訂閱。
  // 取代 useState+useEffect 同步——消除 set-state-in-effect，並符合「外部資料源」的 React 正解。
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onStoreChange);
      return () => mql.removeEventListener("change", onStoreChange);
    },
    [query],
  );

  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(query).matches,
    () => false,
  );
}
