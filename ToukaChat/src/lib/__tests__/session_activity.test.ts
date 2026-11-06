import { describe, expect, it } from 'vitest';
import { isBusyStatus, mostUrgentStatus, parseSessionActivity, type SessionWorkStatus } from '../session_activity';

const AWAITING: SessionWorkStatus = { kind: 'awaiting' };
const REPLYING: SessionWorkStatus = { kind: 'replying' };
const HEARTBEAT: SessionWorkStatus = { kind: 'heartbeat', current: 2, total: 5 };
const UNSEEN: SessionWorkStatus = { kind: 'unseen' };

describe('mostUrgentStatus：等您處理 ＞ 工作中 ＞ 小點', () => {
  it.each([
    [[UNSEEN, REPLYING, AWAITING], AWAITING],
    [[UNSEEN, HEARTBEAT], HEARTBEAT],
    [[UNSEEN, null], UNSEEN],
    [[REPLYING, HEARTBEAT], REPLYING],
    [[null, undefined], null],
    [[], null],
  ] as const)('%j → %j', (statuses, expected) => {
    expect(mostUrgentStatus([...statuses])).toEqual(expected);
  });
});

describe('isBusyStatus：小點不算在工作', () => {
  it.each([
    [AWAITING, true],
    [REPLYING, true],
    [HEARTBEAT, true],
    [UNSEEN, false],
    [null, false],
  ] as const)('%j → %s', (status, expected) => {
    expect(isBusyStatus(status)).toBe(expected);
  });
});

describe('parseSessionActivity：看不懂的回應當成查詢失敗', () => {
  const valid = {
    streams: ['s1'],
    background: [{ session_id: 'b1', kind: 'heartbeat', current: 2, total: 5 }],
    finished: [{ session_id: 'f1', finished_at: 10 }],
    cursor: 10,
  };

  it('接受合約裡的形狀', () => {
    expect(parseSessionActivity(valid)).toEqual(valid);
  });

  it.each([
    ['不是物件', null],
    ['陣列', []],
    ['沒有 cursor', { ...valid, cursor: undefined }],
    ['cursor 不是整數', { ...valid, cursor: 1.5 }],
    ['streams 不是字串陣列', { ...valid, streams: [1] }],
    ['background 缺進度', { ...valid, background: [{ session_id: 'b1', kind: 'heartbeat' }] }],
    ['finished 缺時間', { ...valid, finished: [{ session_id: 'f1' }] }],
  ])('%s', (_name, value) => {
    expect(parseSessionActivity(value)).toBeNull();
  });
});
