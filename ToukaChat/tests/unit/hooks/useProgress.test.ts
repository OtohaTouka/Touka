import { describe, it, expect } from 'vitest';
import { reduceProgress, describeProgress } from '@/hooks/useProgress';
import { ProgressMap } from '@/lib/tree_history';
import i18n from '@/lib/i18n';

describe('reduceProgress', () => {
  it('merges a touka_progress event keyed by kind', () => {
    const next = reduceProgress(undefined, {
      type: 'touka_progress', kind: 'react_slots', current: 1, total: 5,
    });
    expect(next.react_slots).toEqual({ kind: 'react_slots', current: 1, total: 5 });
  });

  it('keeps only the latest state per kind (overwrites same kind)', () => {
    let map: ProgressMap = {};
    map = reduceProgress(map, { type: 'touka_progress', kind: 'react_slots', current: 1, total: 5 });
    map = reduceProgress(map, { type: 'touka_progress', kind: 'react_slots', current: 3, total: 5 });
    expect(map.react_slots).toEqual({ kind: 'react_slots', current: 3, total: 5 });
    expect(Object.keys(map)).toHaveLength(1);
  });

  it('tracks multiple kinds independently', () => {
    let map: ProgressMap = {};
    map = reduceProgress(map, { type: 'touka_progress', kind: 'react_slots', current: 2, total: 5 });
    map = reduceProgress(map, { type: 'touka_progress', kind: 'repair', attempt: 1, max: 3 });
    expect(Object.keys(map).sort()).toEqual(['react_slots', 'repair']);
    expect(map.repair).toEqual({ kind: 'repair', attempt: 1, max: 3 });
  });

  it('ignores non-progress events and缺 kind 者，原樣回傳同一參考', () => {
    const base: ProgressMap = { react_slots: { kind: 'react_slots', current: 1, total: 5 } };
    expect(reduceProgress(base, { type: 'tool_start', id: 't1' })).toBe(base);
    expect(reduceProgress(base, { type: 'touka_progress' })).toBe(base); // 缺 kind
  });

  it('strips the outer type field from the stored entry', () => {
    const next = reduceProgress(undefined, {
      type: 'touka_progress', kind: 'group', index: 2, total: 3, agent: 'Bob',
    });
    expect(next.group).not.toHaveProperty('type');
    expect(next.group.kind).toBe('group');
  });
});

describe('describeProgress', () => {
  // i18n 後 describeProgress(p, t)：t 由呼叫端注入。測試以固定 zh-TW 的 i18n.t 驗證來源中文。
  const t = i18n.t;
  it('labels each kind in Traditional Chinese', () => {
    expect(describeProgress({ kind: 'react_slots', current: 2, total: 5 }, t)).toBe('自主回合 2/5');
    expect(describeProgress({ kind: 'repair', attempt: 1, max: 3 }, t)).toBe('自我修復 1/3');
    expect(describeProgress({ kind: 'group', index: 2, total: 3, agent: 'Bob' }, t)).toBe('群聊 2/3 @Bob');
    expect(describeProgress({ kind: 'creation_stage', stage: 'SMOKE', status: 'ok' }, t)).toBe('創建：SMOKE（ok）');
    expect(describeProgress({ kind: 'tool_retry', tool: 'research', attempt: 3, max: 4 }, t)).toBe('research 重試 3/4');
    expect(describeProgress({ kind: 'token_usage', total: 1200, cost: 0.0036 }, t)).toBe('本回合 1,200 tok · $0.0036');
  });

  it('falls back to the raw kind for unknown kinds', () => {
    expect(describeProgress({ kind: 'mystery' }, t)).toBe('mystery');
  });
});
