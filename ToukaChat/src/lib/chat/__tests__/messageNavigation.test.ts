/**
 * 對話訊息導覽列的純函式：哪些訊息畫線、預覽文字、分段、目前那則。
 */
import { describe, it, expect } from 'vitest';
import type { MessageNode } from '@/lib/tree_history';
import {
  SYSTEM_SPEAKER, buildNavEntries, groupNavEntries, maxTicksFor, pickCurrentIndex, isTickCurrent, type NavEntry,
} from '../messageNavigation';

function node(id: string, role: MessageNode['role'], content: string, extra: Partial<MessageNode> = {}): MessageNode {
  return { id, parentId: null, childrenIds: [], role, content, ...extra };
}

function entries(count: number): NavEntry[] {
  // 使用者與助理交替：使用者在偶數 index
  const linear = Array.from({ length: count * 2 }, (_, i) =>
    node(`n${i}`, i % 2 === 0 ? 'user' : 'assistant', `m${i}`));
  return buildNavEntries(linear);
}

describe('buildNavEntries', () => {
  it('只取使用者訊息，記下在線性路徑裡的位置', () => {
    const linear = [
      node('u1', 'user', '第一句'),
      node('a1', 'assistant', '回覆'),
      node('s1', 'system', '系統'),
      node('u2', 'user', '第二句'),
    ];
    expect(buildNavEntries(linear).map(e => [e.id, e.index])).toEqual([['u1', 0], ['u2', 3]]);
  });

  it('排除心跳補的系統訊息；心跳開頭提示與 SNS 外人訊息照畫', () => {
    const linear = [
      node('init', 'user', '【目前系統時間：2026-10-29 10:00 (週四)】\n\n看一下進度'),
      node('a1', 'assistant', '好'),
      node('nudge', 'user', '【脈衝能量補給：10:01】\n\n請繼續執行您的任務或檢查進度。', { name: SYSTEM_SPEAKER }),
      node('a2', 'assistant', '繼續'),
      node('stage', 'user', '【系統】自主行動階段完成，請繼續。', { name: SYSTEM_SPEAKER }),
      node('a3', 'assistant', '完成'),
      node('sns', 'user', '【SNS 外部訊息】\nhi'),
    ];
    expect(buildNavEntries(linear).map(e => e.id)).toEqual(['init', 'sns']);
  });

  it('其他名字的使用者訊息照畫', () => {
    const linear = [node('u1', 'user', 'hi', { name: 'Otoha' })];
    expect(buildNavEntries(linear).map(e => e.id)).toEqual(['u1']);
  });

  it('預覽去掉頭尾空白，太長就截斷加刪節號', () => {
    const long = 'あ'.repeat(500);
    const [short, cut] = buildNavEntries([
      node('u1', 'user', '  第一行\n第二行  '),
      node('u2', 'user', long),
    ]);
    expect(short.preview).toBe('第一行\n第二行');
    expect(cut.preview.endsWith('…')).toBe(true);
    expect(cut.preview.length).toBeLessThan(long.length);
    expect(cut.preview.slice(0, -1)).toBe(long.slice(0, cut.preview.length - 1));
  });

  it('內容缺漏時預覽是空字串', () => {
    const missing = { ...node('u1', 'user', ''), content: undefined } as unknown as MessageNode;
    expect(buildNavEntries([missing])[0].preview).toBe('');
  });

  it('附件分成圖片與檔案，檔案沒有檔名時記 null', () => {
    const [image, file, unnamed, none] = buildNavEntries([
      node('u1', 'user', '', { attachment: { type: 'image', url: 'data:x' } }),
      node('u2', 'user', '', { attachment: { type: 'text', url: '', name: 'notes.md' } }),
      node('u3', 'user', '', { attachment: { type: 'text', url: '' } }),
      node('u4', 'user', 'hi'),
    ]);
    expect(image.attachment).toEqual({ kind: 'image' });
    expect(file.attachment).toEqual({ kind: 'file', name: 'notes.md' });
    expect(unnamed.attachment).toEqual({ kind: 'file', name: null });
    expect(none.attachment).toBeNull();
  });
});

describe('groupNavEntries', () => {
  it('沒超過上限時一則一條', () => {
    const ticks = groupNavEntries(entries(3), 3);
    expect(ticks.map(t => [t.index, t.startIndex, t.endIndex, t.firstOrdinal, t.lastOrdinal]))
      .toEqual([[0, 0, 0, 1, 1], [2, 2, 2, 2, 2], [4, 4, 4, 3, 3]]);
  });

  it('超過上限時平均分成上限條，每條用該段第一則', () => {
    const ticks = groupNavEntries(entries(7), 3);
    // 7 則分 3 段：0–1、2–3、4–6（第幾則從 1 算）
    expect(ticks.map(t => [t.firstOrdinal, t.lastOrdinal])).toEqual([[1, 2], [3, 4], [5, 7]]);
    expect(ticks.map(t => [t.index, t.startIndex, t.endIndex])).toEqual([[0, 0, 2], [4, 4, 6], [8, 8, 12]]);
    expect(ticks.map(t => t.entry.id)).toEqual(['n0', 'n4', 'n8']);
  });

  it('沒有訊息時沒有線', () => {
    expect(groupNavEntries([], 3)).toEqual([]);
  });
});

describe('maxTicksFor', () => {
  it.each([
    ['還沒量到高度時用上限', null, 120],
    ['高度是 0（還沒排版）時用上限', 0, 120],
    ['每條至少 8px：500px 排 62 條', 500, 62],
    ['剛好整除', 400, 50],
    ['很高時不超過上限', 2000, 120],
    ['很矮時至少 2 條', 10, 2],
  ])('%s', (_name, height, expected) => {
    expect(maxTicksFor(height)).toBe(expected);
  });
});

describe('pickCurrentIndex', () => {
  const list = entries(4); // 使用者在 0、2、4、6

  it.each([
    ['範圍內有多則時取最上面那則', { startIndex: 1, endIndex: 5 }, 2],
    ['範圍剛好從使用者訊息開始', { startIndex: 4, endIndex: 7 }, 4],
    ['範圍內沒有使用者訊息時取前面最近的', { startIndex: 5, endIndex: 5 }, 4],
    ['範圍在第一則之前時沒有', { startIndex: -3, endIndex: -1 }, null],
  ])('%s', (_name, range, expected) => {
    expect(pickCurrentIndex(list, range)).toBe(expected);
  });

  it('還不知道範圍時沒有', () => {
    expect(pickCurrentIndex(list, null)).toBeNull();
  });
});

describe('isTickCurrent', () => {
  it('分段時目前那則落在段內（不是第一則）整段都算', () => {
    const [first, second] = groupNavEntries(entries(6), 2); // 0–4、6–10
    expect(isTickCurrent(first, 2)).toBe(true);
    expect(isTickCurrent(first, 4)).toBe(true);
    expect(isTickCurrent(second, 4)).toBe(false);
    expect(isTickCurrent(second, 8)).toBe(true);
  });

  it('沒有目前那則時都不算', () => {
    const [tick] = groupNavEntries(entries(1), 1);
    expect(isTickCurrent(tick, null)).toBe(false);
  });
});
