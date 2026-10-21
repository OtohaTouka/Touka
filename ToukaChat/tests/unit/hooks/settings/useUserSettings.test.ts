import { renderHook } from '@testing-library/react';
import { useUserSettings } from '@/hooks/settings/useUserSettings';
import { describe, it, expect, vi } from 'vitest';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://localhost:8000',
  fetchTouka: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve([]) })),
}));

describe('useUserSettings deleteUser', () => {
  it('does not invoke window.confirm at hook layer (M8)', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockImplementation(() => true);
    const { result } = renderHook(() => useUserSettings());

    // Call deleteUser
    await result.current.deleteUser('test-user-id');

    // Confirm should NOT be called at the hook layer
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});
