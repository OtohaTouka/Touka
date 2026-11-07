import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { useUnseenSessions } from '../useUnseenSessions';

const KEY = 'touka_session_unseen:user-1';
const stored = () => JSON.parse(window.localStorage.getItem(KEY) ?? 'null');

describe('useUnseenSessions：瀏覽器裡的值壞掉（不是陣列）時當成空的', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.localStorage.setItem(KEY, JSON.stringify({ broken: true }));
  });

  it('記一筆', () => {
    const { result } = renderHook(() => useUnseenSessions('user-1'));
    act(() => result.current.markUnseen('a', true));
    expect(stored()).toEqual(['a']);
    expect([...result.current.unseenIds]).toEqual(['a']);
  });

  it('清一筆', () => {
    const { result } = renderHook(() => useUnseenSessions('user-1'));
    act(() => result.current.clearUnseen('a'));
    expect(stored()).toEqual([]);
  });

  it('只留還用得到的', () => {
    const { result } = renderHook(() => useUnseenSessions('user-1'));
    act(() => result.current.pruneUnseen(() => true));
    expect(stored()).toEqual([]);
  });
});
