import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { usePendingRequestQueue } from '../usePendingRequestQueue';

const request = (id: string) => ({ id });

describe('usePendingRequestQueue', () => {
  it('對外是目前那一筆，第二個不會蓋掉第一個；關掉跳下一筆', () => {
    const { result } = renderHook(() => usePendingRequestQueue<{ id: string }>());
    expect(result.current.current).toBeNull();
    act(() => result.current.enqueue('s1', request('r1')));
    act(() => result.current.enqueue('s2', request('r2')));
    expect(result.current.current?.id).toBe('r1');
    expect(result.current.sessionIds).toEqual(['s1', 's2']);
    act(() => result.current.dismiss('r1'));
    expect(result.current.current?.id).toBe('r2');
    expect(result.current.sessionIds).toEqual(['s2']);
    act(() => result.current.dismiss('r2'));
    expect(result.current.current).toBeNull();
  });

  it('依編號關：那筆已經被移除時，不會關到排在後面的', () => {
    const { result } = renderHook(() => usePendingRequestQueue<{ id: string }>());
    act(() => result.current.enqueue('s1', request('r1')));
    act(() => result.current.enqueue('s2', request('r2')));
    act(() => result.current.dropSession('s1'));
    act(() => result.current.dismiss('r1'));
    expect(result.current.current?.id).toBe('r2');
    expect(result.current.sessionIds).toEqual(['s2']);
  });

  it('同一筆請求重送不會排兩次', () => {
    const { result } = renderHook(() => usePendingRequestQueue<{ id: string }>());
    act(() => result.current.enqueue('s1', request('r1')));
    act(() => result.current.enqueue('s1', request('r1')));
    expect(result.current.sessionIds).toEqual(['s1']);
  });

  it('串流結束的那段對話，排隊中與正在顯示的都移除', () => {
    const { result } = renderHook(() => usePendingRequestQueue<{ id: string }>());
    act(() => result.current.enqueue('s1', request('r1')));
    act(() => result.current.enqueue('s2', request('r2')));
    act(() => result.current.enqueue('s1', request('r3')));
    act(() => result.current.dropSession('s1'));
    expect(result.current.current?.id).toBe('r2');
    expect(result.current.sessionIds).toEqual(['s2']);
  });
});
