import { renderHook, act } from '@testing-library/react';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { describe, it, expect } from 'vitest';

describe('useLocalStorage stable setter', () => {
  it('returns a stable setter function reference (M5)', () => {
    const { result, rerender } = renderHook(() => useLocalStorage('test_key', 'initial'));
    const firstSetter = result.current[1];

    // Trigger a re-render
    rerender();
    const secondSetter = result.current[1];

    // Reference should be stable (identical)
    expect(firstSetter).toBe(secondSetter);

    // Call setter and verify it works
    act(() => {
      firstSetter('new_value');
    });
    expect(result.current[0]).toBe('new_value');
  });
});
