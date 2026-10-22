import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import type { AutonomyConfig } from '@/lib/types';

vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://x' }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

import { fetchTouka } from '@/lib/connections';
import { REVIEWER_TIMEOUT_MAX_S, useAutoReviewSettings } from '../useAutoReviewSettings';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;
const withReview = { auto_review: { model: '', max_tokens: 512, timeout_s: 20, headless_terminal: false } } as unknown as AutonomyConfig;

describe('useAutoReviewSettings（ADR-0246）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('rejects a malformed list instead of showing unknown entries', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => ({ models: [{ id: 'x' }] }) });
    const { result } = renderHook(() => useAutoReviewSettings(withReview, vi.fn()));
    await waitFor(() => expect(result.current.loadFailed).toBe(true));
    expect(result.current.models).toBeNull();
  });

  it('a network error is logged and reported as a load failure', async () => {
    mockFetch.mockRejectedValue(new Error('network'));
    const { result } = renderHook(() => useAutoReviewSettings(withReview, vi.fn()));
    await waitFor(() => expect(result.current.loadFailed).toBe(true));
  });

  it('does nothing when the config has no auto_review block', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => ({ models: [] }) });
    const setConfig = vi.fn();
    const { result } = renderHook(() => useAutoReviewSettings({} as AutonomyConfig, setConfig));
    await waitFor(() => expect(result.current.models).toEqual([]));
    act(() => result.current.setReviewerModel('claude-haiku-5-5'));
    expect(setConfig).not.toHaveBeenCalled();
    expect(result.current.editable).toBe(false);
    expect(result.current.selectedModel).toBe('');
  });

  // r55：審查員等待秒數
  it('setReviewerTimeout changes only timeout_s and keeps the other reviewer fields', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => ({ models: [] }) });
    const setConfig = vi.fn();
    const current = {
      react_loop: { max_tool_rounds: 3 },
      auto_review: { model: 'claude-haiku-5-5', max_tokens: 300, timeout_s: 20, headless_terminal: true },
    } as unknown as AutonomyConfig;
    const { result } = renderHook(() => useAutoReviewSettings(current, setConfig));
    await waitFor(() => expect(result.current.models).toEqual([]));
    expect(result.current.timeoutS).toBe(20);
    act(() => result.current.setReviewerTimeout(60));
    expect(setConfig).toHaveBeenCalledWith({
      react_loop: { max_tool_rounds: 3 },
      auto_review: { model: 'claude-haiku-5-5', max_tokens: 300, timeout_s: 60, headless_terminal: true },
    });
  });

  it('setReviewerTimeout does nothing without an auto_review block', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => ({ models: [] }) });
    const setConfig = vi.fn();
    const { result } = renderHook(() => useAutoReviewSettings({} as AutonomyConfig, setConfig));
    await waitFor(() => expect(result.current.models).toEqual([]));
    act(() => result.current.setReviewerTimeout(60));
    expect(setConfig).not.toHaveBeenCalled();
    expect(result.current.timeoutValid).toBe(false);
  });

  it.each([
    [1, true], [0.5, true], [60, true], [REVIEWER_TIMEOUT_MAX_S, true],
    [0, false], [-1, false], [120.5, false], [Number.NaN, false], [Number.POSITIVE_INFINITY, false],
  ])('timeoutValid(%s) is %s (backend: gt=0, le=120)', async (timeout_s, valid) => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => ({ models: [] }) });
    const config = { auto_review: { ...withReview.auto_review!, timeout_s } } as AutonomyConfig;
    const { result } = renderHook(() => useAutoReviewSettings(config, vi.fn()));
    await waitFor(() => expect(result.current.models).toEqual([]));
    expect(result.current.timeoutValid).toBe(valid);
  });

  it('the upper bound matches the backend limit of 120 seconds', () => {
    expect(REVIEWER_TIMEOUT_MAX_S).toBe(120);
  });
});
