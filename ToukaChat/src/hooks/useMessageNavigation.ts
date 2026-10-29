"use client";

import { useEffect, useMemo, useState, type RefObject } from 'react';
import type { MessageNode } from '@/lib/tree_history';
import {
  buildNavEntries, groupNavEntries, maxTicksFor, pickCurrentIndex, type NavRange, type NavTick,
} from '@/lib/chat/messageNavigation';

export interface MessageNavigation {
  ticks: NavTick[];
  currentIndex: number | null;
  /** 給 Virtuoso 的 rangeChanged；停用時為 undefined，就不訂閱捲動範圍。 */
  onRangeChanged: ((range: NavRange) => void) | undefined;
}

/**
 * 對話左側訊息導覽列的資料：從 ChatArea 同一份線性路徑算出線，依捲動範圍算出目前那則。
 * 範圍存 index（不存訊息 id），換對話時同一個範圍也會照新的路徑重算。
 * 線數依導覽區（containerRef）的高度調整，矮的視窗也分得開每一條。
 */
export function useMessageNavigation(
  linearMessages: MessageNode[], enabled: boolean, containerRef: RefObject<HTMLElement | null>,
): MessageNavigation {
  const [range, setRange] = useState<NavRange | null>(null);
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
  const currentIndex = useMemo(() => pickCurrentIndex(entries, range), [entries, range]);
  return { ticks, currentIndex, onRangeChanged: enabled ? setRange : undefined };
}
