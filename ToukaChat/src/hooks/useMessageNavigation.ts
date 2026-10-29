"use client";

import { useLayoutEffect, useMemo, useState, useSyncExternalStore, type RefObject } from 'react';
import { flushSync } from 'react-dom';
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

  // layout effect：畫面畫出之前先量一次，多數情況第一個畫面就是對的條數；
  // 但這一刻其他版面（例如下方輸入區）可能還沒定下來，最後的高度靠 ResizeObserver 補上
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!enabled || !container) return;
    // 存條數而不是高度：拖動視窗時條數沒變就不重繪。量不到高度（0）時用上限
    setMaxTicks(maxTicksFor(container.clientHeight));
    // 沒有 ResizeObserver 時只量這一次，之後不跟著變
    if (typeof ResizeObserver === 'undefined') return;
    // ResizeObserver 在繪製前通知，但一般的 setState 會排到繪製之後，第一個畫面會先畫舊條數；
    // flushSync 當場重繪，瀏覽器在同一次繪製前重新排版。條數沒變時 React 直接略過；條數變了（拉動視窗約每 8px 一次）
    // ChatArea 會在這一格繪製前同步重繪，工作量和排到之後一樣，只是提前做
    const observer = new ResizeObserver(([entry]) => {
      flushSync(() => setMaxTicks(maxTicksFor(entry.contentRect.height)));
    });
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
