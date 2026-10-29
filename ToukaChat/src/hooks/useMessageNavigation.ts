"use client";

import { useEffect, useMemo, useState, useSyncExternalStore, type RefObject } from 'react';
import type { MessageNode } from '@/lib/tree_history';
import {
  buildNavEntries, groupNavEntries, maxTicksFor, pickCurrentIndex, type NavRange, type NavTick,
} from '@/lib/chat/messageNavigation';

export interface MessageNavigation {
  ticks: NavTick[];
  currentIndex: number | null;
  /** 給 Virtuoso 的 rangeChanged；停用時照樣收（不重繪），重新啟用時標亮立刻正確。 */
  onRangeChanged: (range: NavRange) => void;
}

/** 最新的捲動範圍放在 React state 外面：範圍每變一次就重繪整個 ChatArea 太浪費。 */
interface RangeStore {
  get: () => NavRange | null;
  set: (range: NavRange) => void;
  subscribe: (listener: () => void) => () => void;
}

function createRangeStore(): RangeStore {
  let range: NavRange | null = null;
  const listeners = new Set<() => void>();
  return {
    get: () => range,
    set: next => {
      range = next;
      listeners.forEach(listener => listener());
    },
    subscribe: listener => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/**
 * 對話左側訊息導覽列的資料：從 ChatArea 同一份線性路徑算出線，依捲動範圍算出目前那則。
 * 範圍存 index（不存訊息 id），換對話時同一個範圍也會照新的路徑重算；
 * 目前那則沒變時不重繪。線數依導覽區（containerRef）的高度調整，矮的視窗也分得開每一條。
 */
export function useMessageNavigation(
  linearMessages: MessageNode[], enabled: boolean, containerRef: RefObject<HTMLElement | null>,
): MessageNavigation {
  const [rangeStore] = useState(createRangeStore);
  const [maxTicks, setMaxTicks] = useState(() => maxTicksFor(null));

  useEffect(() => {
    const container = containerRef.current;
    // 沒有 ResizeObserver（jsdom）時維持上限
    if (!enabled || !container || typeof ResizeObserver === 'undefined') return;
    // 存條數而不是高度：拖動視窗時條數沒變就不重繪
    const observer = new ResizeObserver(([entry]) => setMaxTicks(maxTicksFor(entry.contentRect.height)));
    observer.observe(container);
    return () => observer.disconnect();
  }, [enabled, containerRef]);

  const entries = useMemo(() => buildNavEntries(linearMessages), [linearMessages]);
  const ticks = useMemo(() => groupNavEntries(entries, maxTicks), [entries, maxTicks]);
  // 回傳的是數字：範圍變了但算出來同一則，React 就不重繪
  const currentIndex = useSyncExternalStore(
    rangeStore.subscribe,
    () => (enabled ? pickCurrentIndex(entries, rangeStore.get()) : null),
    () => null,
  );
  return { ticks, currentIndex, onRangeChanged: rangeStore.set };
}
