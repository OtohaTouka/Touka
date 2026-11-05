import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { useSessionTreeState } from '../useSessionTreeState';

const KEY = 'touka_session_tree_collapsed:user-1';

beforeEach(() => {
  window.localStorage.clear();
});

describe('useSessionTreeState', () => {
  it('預設全部展開', () => {
    const { result } = renderHook(() => useSessionTreeState('user-1'));
    expect(result.current.isCollapsed('alice')).toBe(false);
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });

  it('收合寫進瀏覽器，重掛後還在；展開就清掉', () => {
    const first = renderHook(() => useSessionTreeState('user-1'));
    act(() => first.result.current.setCollapsed('alice', true, true));
    expect(first.result.current.isCollapsed('alice')).toBe(true);
    first.unmount();

    const second = renderHook(() => useSessionTreeState('user-1'));
    expect(second.result.current.isCollapsed('alice')).toBe(true);
    act(() => second.result.current.setCollapsed('alice', false, true));
    expect(second.result.current.isCollapsed('alice')).toBe(false);
    expect(JSON.parse(window.localStorage.getItem(KEY) ?? 'null')).toEqual({});
  });

  it('各帳號分開記', () => {
    const one = renderHook(() => useSessionTreeState('user-1'));
    act(() => one.result.current.setCollapsed('alice', true, true));
    const two = renderHook(() => useSessionTreeState('user-2'));
    expect(two.result.current.isCollapsed('alice')).toBe(false);
  });

  it('不寫瀏覽器的（只有無痕的資料夾）只記在記憶體，重掛就沒了', () => {
    const first = renderHook(() => useSessionTreeState('user-1'));
    act(() => first.result.current.setCollapsed('ghost', true, false));
    expect(first.result.current.isCollapsed('ghost')).toBe(true);
    expect(window.localStorage.getItem(KEY)).toBeNull();
    first.unmount();
    expect(renderHook(() => useSessionTreeState('user-1')).result.current.isCollapsed('ghost')).toBe(false);
  });

  it('資料夾之後有了一般對話：改寫瀏覽器時蓋過記憶體的值', () => {
    const { result } = renderHook(() => useSessionTreeState('user-1'));
    act(() => result.current.setCollapsed('alice', true, false));
    act(() => result.current.setCollapsed('alice', false, true));
    expect(result.current.isCollapsed('alice')).toBe(false);
  });

  it('瀏覽器裡的值壞掉時當作沒有', () => {
    window.localStorage.setItem(KEY, 'null');
    const { result } = renderHook(() => useSessionTreeState('user-1'));
    expect(result.current.isCollapsed('alice')).toBe(false);
    act(() => result.current.setCollapsed('alice', true, true));
    expect(result.current.isCollapsed('alice')).toBe(true);
  });

  it('「還有 N 個」按過就全列，重掛後恢復，不寫瀏覽器', () => {
    const first = renderHook(() => useSessionTreeState('user-1'));
    expect(first.result.current.isShowingAll('alice')).toBe(false);
    act(() => first.result.current.showAll('alice'));
    expect(first.result.current.isShowingAll('alice')).toBe(true);
    expect(window.localStorage.getItem(KEY)).toBeNull();
    first.unmount();
    expect(renderHook(() => useSessionTreeState('user-1')).result.current.isShowingAll('alice')).toBe(false);
  });
});
