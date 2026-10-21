import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { useSessionActions } from '@/hooks/useSessionActions';
import type { Session } from '@/lib/types';
import * as SessionProvider from '@/lib/providers/SessionListProvider';
import * as ChatProvider from '@/lib/providers/ChatProvider';
import * as ModelProvider from '@/lib/providers/ModelProvider';
import * as exportUtils from '@/lib/export-utils';
import * as connections from '@/lib/connections';

vi.mock('react-i18next', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-i18next')>(),
  useTranslation: vi.fn(),
}));
vi.mock('@/lib/providers/SessionListProvider');
vi.mock('@/lib/providers/ChatProvider');
vi.mock('@/lib/providers/ModelProvider');
vi.mock('@/lib/export-utils');
vi.mock('@/lib/connections');

const homeSession: Session = {
  id: 'home-session', title: 'Home', messages: [], updatedAt: 0, is_home: true,
};
const shortSession: Session = {
  id: 'short-session', title: 'Short', messages: [], updatedAt: 0,
};
const tA = (key: string) => `A:${key}`;
const tB = (key: string) => `B:${key}`;

describe('P11 useSessionActions 翻譯回呼', () => {
  let deleteSession: ReturnType<typeof vi.fn>;
  let updateSessionMessages: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    deleteSession = vi.fn();
    updateSessionMessages = vi.fn();
    vi.mocked(useTranslation).mockReturnValue({ t: tA } as never);
    vi.mocked(SessionProvider.useSessionList).mockReturnValue({
      sessions: [homeSession, shortSession],
      deleteSession,
      updateSessionTitle: vi.fn(),
    } as never);
    vi.mocked(ChatProvider.useChat).mockReturnValue({
      handleUpdateSessionMessages: updateSessionMessages,
    } as never);
    vi.mocked(ModelProvider.useModels).mockReturnValue({ selectedModel: 'gpt-4o' } as never);
    vi.mocked(exportUtils.splitSessionMessages).mockReturnValue(null);
  });

  it('切換語言後，家對話禁止刪除提示使用新翻譯且不刪除', () => {
    const warnSpy = vi.spyOn(toast, 'warning').mockReturnValue('toast-id');
    const { result, rerender } = renderHook(() => useSessionActions());

    act(() => { result.current.handleDelete(homeSession.id); });
    expect(warnSpy).toHaveBeenLastCalledWith('A:hooks.use_session_actions.home_session_cannot_delete');

    vi.mocked(useTranslation).mockReturnValue({ t: tB } as never);
    rerender();
    act(() => { result.current.handleDelete(homeSession.id); });

    expect(warnSpy).toHaveBeenLastCalledWith('B:hooks.use_session_actions.home_session_cannot_delete');
    expect(result.current.deleteConfirmId).toBeNull();
    expect(deleteSession).not.toHaveBeenCalled();
  });

  it('切換語言後，過短對話不分割提示使用新翻譯且不匯出', async () => {
    const infoSpy = vi.spyOn(toast, 'info').mockReturnValue('toast-id');
    const { result, rerender } = renderHook(() => useSessionActions());

    await act(async () => { await result.current.handleSplitExport(shortSession); });
    expect(infoSpy).toHaveBeenLastCalledWith('A:hooks.use_session_actions.session_too_small_to_split');

    vi.mocked(useTranslation).mockReturnValue({ t: tB } as never);
    rerender();
    await act(async () => { await result.current.handleSplitExport(shortSession); });

    expect(infoSpy).toHaveBeenLastCalledWith('B:hooks.use_session_actions.session_too_small_to_split');
    expect(exportUtils.splitSessionMessages).toHaveBeenLastCalledWith(shortSession, 30);
    expect(exportUtils.formatToOpenWebUI).not.toHaveBeenCalled();
    expect(exportUtils.triggerDownload).not.toHaveBeenCalled();
    expect(connections.fetchTouka).not.toHaveBeenCalled();
    expect(updateSessionMessages).not.toHaveBeenCalled();
    expect(deleteSession).not.toHaveBeenCalled();
  });
});
