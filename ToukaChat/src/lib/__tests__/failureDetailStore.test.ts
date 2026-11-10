/**
 * 廠商錯誤原文只放記憶體（路線圖第 8 項 PR 1）：對話樹會存檔、會和存檔比對，原文不能進去。
 */
import { afterEach, describe, expect, it } from 'vitest';
import { MAX_FAILURE_DETAILS, rememberFailureDetail, useFailureDetails } from '../chat/failureDetailStore';
import { reduceProgress, GENERATION_FAILURE_KIND } from '@/hooks/useProgress';

const failure = (detail?: unknown) => ({
  type: 'touka_progress', kind: GENERATION_FAILURE_KIND, error_type: 'rate_limit', detail,
});
const details = () => useFailureDetails.getState().details;

describe('rememberFailureDetail', () => {
  afterEach(() => useFailureDetails.setState({ details: {} }));

  it('記下這則回覆的廠商原文', () => {
    rememberFailureDetail('a1', failure('quota used up'));
    expect(details()).toEqual({ a1: 'quota used up' });
  });

  it.each([
    [{ type: 'touka_progress', kind: 'token_usage', detail: 'x' }],
    [{ type: 'other', kind: GENERATION_FAILURE_KIND, detail: 'x' }],
    [failure()],
    [failure('')],
    [failure(3)],
    [failure({ nested: true })],
  ])('其他事件或不是字串的原文不記 %#', (event) => {
    rememberFailureDetail('a1', event as Record<string, unknown>);
    expect(details()).toEqual({});
  });

  it('同一則回覆以最新的為準', () => {
    rememberFailureDetail('a1', failure('old'));
    rememberFailureDetail('a1', failure('new'));
    expect(details()).toEqual({ a1: 'new' });
  });

  it('只留最近幾則，不會一直長大', () => {
    for (let i = 0; i < MAX_FAILURE_DETAILS + 5; i += 1) rememberFailureDetail(`a${i}`, failure(`d${i}`));
    const kept = details();
    expect(Object.keys(kept)).toHaveLength(MAX_FAILURE_DETAILS);
    expect(kept.a0).toBeUndefined();
    expect(kept[`a${MAX_FAILURE_DETAILS + 4}`]).toBe(`d${MAX_FAILURE_DETAILS + 4}`);
  });

  it('同一則再記一次算最新的，也不會多擠掉別則', () => {
    for (let i = 0; i < MAX_FAILURE_DETAILS; i += 1) rememberFailureDetail(`a${i}`, failure(`d${i}`));
    rememberFailureDetail('a5', failure('again'));
    const kept = details();
    expect(Object.keys(kept)).toHaveLength(MAX_FAILURE_DETAILS);
    expect(kept.a0).toBe('d0');
    expect(Object.keys(kept).at(-1)).toBe('a5');
    expect(kept.a5).toBe('again');
  });
});

describe('原文不進對話樹', () => {
  it('進度表只留分類', () => {
    const map = reduceProgress(undefined, failure('quota used up'));
    expect(map[GENERATION_FAILURE_KIND]).toEqual({ kind: GENERATION_FAILURE_KIND, error_type: 'rate_limit' });
  });

  it('別種進度帶 detail 欄位照舊，不受影響', () => {
    const map = reduceProgress(undefined, { type: 'touka_progress', kind: 'repair', detail: 'kept' });
    expect(map.repair).toEqual({ kind: 'repair', detail: 'kept' });
  });
});
