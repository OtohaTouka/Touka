import { describe, it, expect } from 'vitest';
import { getDispatchedTaskIds } from '@/lib/mission_utils';

describe('getDispatchedTaskIds', () => {
  it('returns task ids that have dispatch callbacks (dispatched to external AI)', () => {
    const ids = getDispatchedTaskIds({ dispatch_callbacks: { t1: 'tok1', t3: 'tok3' } });
    expect(ids.has('t1')).toBe(true);
    expect(ids.has('t3')).toBe(true);
    expect(ids.has('t2')).toBe(false);
    expect(ids.size).toBe(2);
  });

  it('returns an empty set when there is no dispatch metadata', () => {
    expect(getDispatchedTaskIds(undefined).size).toBe(0);
    expect(getDispatchedTaskIds(null).size).toBe(0);
    expect(getDispatchedTaskIds({}).size).toBe(0);
    expect(getDispatchedTaskIds({ dispatch_callbacks: {} }).size).toBe(0);
  });
});
