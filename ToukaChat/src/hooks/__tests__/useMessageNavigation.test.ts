/**
 * 對話訊息導覽列的資料 hook：線跟著線性路徑重算、捲動範圍決定目前那則（沒變就不重繪）、
 * 停用時沒有目前那則，線數依導覽區高度調整。
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type { MessageNode } from '@/lib/tree_history';
import { useMessageNavigation } from '../useMessageNavigation';

function node(id: string, role: MessageNode['role']): MessageNode {
  return { id, parentId: null, childrenIds: [], role, content: id };
}

const FIRST = [node('u1', 'user'), node('a1', 'assistant'), node('u2', 'user'), node('a2', 'assistant')];
const OTHER = [node('x1', 'user'), node('b1', 'assistant')];
const FIFTY = Array.from({ length: 100 }, (_, i) => node(`n${i}`, i % 2 === 0 ? 'user' : 'assistant'));
const NO_CONTAINER = { current: null };

/** jsdom 沒有 ResizeObserver：記下觀察對象，由測試觸發高度變化。 */
class FakeResizeObserver {
  static instances: FakeResizeObserver[] = [];
  observed: Element[] = [];
  disconnected = false;
  constructor(private readonly callback: ResizeObserverCallback) {
    FakeResizeObserver.instances.push(this);
  }
  observe(element: Element) { this.observed.push(element); }
  unobserve() {}
  disconnect() { this.disconnected = true; }
  resize(height: number) {
    this.callback([{ contentRect: { height } } as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
}

function stubResizeObserver() {
  FakeResizeObserver.instances = [];
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useMessageNavigation', () => {
  it('換一份線性路徑後線跟著重算', () => {
    const { result, rerender } = renderHook(({ linear }) => useMessageNavigation(linear, true, NO_CONTAINER), {
      initialProps: { linear: FIRST },
    });
    expect(result.current.ticks.map(t => t.key)).toEqual(['u1', 'u2']);
    rerender({ linear: OTHER });
    expect(result.current.ticks.map(t => t.key)).toEqual(['x1']);
  });

  it('收到捲動範圍後算出目前那則', () => {
    const { result } = renderHook(() => useMessageNavigation(FIRST, true, NO_CONTAINER));
    expect(result.current.currentIndex).toBeNull();
    act(() => result.current.onRangeChanged?.({ startIndex: 3, endIndex: 3 }));
    expect(result.current.currentIndex).toBe(2);
    act(() => result.current.onRangeChanged?.({ startIndex: 0, endIndex: 3 }));
    expect(result.current.currentIndex).toBe(0);
  });

  it('捲動範圍改變但目前那則沒變時不重繪', () => {
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useMessageNavigation(FIRST, true, NO_CONTAINER);
    });
    act(() => result.current.onRangeChanged({ startIndex: 0, endIndex: 1 }));
    expect(result.current.currentIndex).toBe(0);
    const settled = renders;
    act(() => result.current.onRangeChanged({ startIndex: 0, endIndex: 3 }));
    act(() => result.current.onRangeChanged({ startIndex: 1, endIndex: 1 }));
    expect(renders).toBe(settled);
    act(() => result.current.onRangeChanged({ startIndex: 2, endIndex: 3 }));
    expect(result.current.currentIndex).toBe(2);
    expect(renders).toBe(settled + 1);
  });

  it('換路徑時用最新的捲動範圍重算目前那則', () => {
    const LATE = [node('a0', 'assistant'), node('a1', 'assistant'), node('a2', 'assistant'), node('u3', 'user')];
    const { result, rerender } = renderHook(({ linear }) => useMessageNavigation(linear, true, NO_CONTAINER), {
      initialProps: { linear: FIRST },
    });
    act(() => result.current.onRangeChanged({ startIndex: 0, endIndex: 1 }));
    act(() => result.current.onRangeChanged({ startIndex: 2, endIndex: 3 }));
    rerender({ linear: LATE });
    expect(result.current.currentIndex).toBe(3);
  });

  it('停用時沒有目前那則；重新啟用時立刻用停用期間收到的範圍（手機版拉回桌面）', () => {
    const { result, rerender } = renderHook(({ enabled }) => useMessageNavigation(FIRST, enabled, NO_CONTAINER), {
      initialProps: { enabled: false },
    });
    act(() => result.current.onRangeChanged?.({ startIndex: 3, endIndex: 3 }));
    expect(result.current.currentIndex).toBeNull();
    rerender({ enabled: true });
    expect(result.current.currentIndex).toBe(2);
  });

  it('量到導覽區高度後線數跟著調整，卸載時停止觀察', () => {
    stubResizeObserver();
    const container = { current: document.createElement('div') };
    const { result, unmount } = renderHook(() => useMessageNavigation(FIFTY, true, container));
    expect(result.current.ticks).toHaveLength(50);
    const [observer] = FakeResizeObserver.instances;
    expect(observer.observed).toEqual([container.current]);
    act(() => observer.resize(200)); // 每 8px 一條：25 條
    expect(result.current.ticks).toHaveLength(25);
    expect(result.current.ticks[1].firstOrdinal).toBe(3);
    act(() => observer.resize(800));
    expect(result.current.ticks).toHaveLength(50);
    unmount();
    expect(observer.disconnected).toBe(true);
  });

  it('停用時不觀察高度', () => {
    stubResizeObserver();
    const container = { current: document.createElement('div') };
    renderHook(() => useMessageNavigation(FIFTY, false, container));
    expect(FakeResizeObserver.instances).toHaveLength(0);
  });

  it('沒有 ResizeObserver 時照上限畫線', () => {
    vi.stubGlobal('ResizeObserver', undefined);
    const container = { current: document.createElement('div') };
    const { result } = renderHook(() => useMessageNavigation(FIFTY, true, container));
    expect(result.current.ticks).toHaveLength(50);
  });
});
