import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { toast } from 'sonner';

vi.mock('@/lib/connections', () => ({
  captureRequestScope: () => ({ proxyOrigin: 'http://synthetic', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true,
}));

// 匯入確認門檻（覆蓋警告）：此處自動確認，讓「有效匯入會合併」的既有行為維持可測。
vi.mock('@/hooks/useConfirmToast', () => ({
  useConfirmToast: () => ({
    confirmToast: (_m: string, onConfirm: () => void) => { onConfirm(); },
  }),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));

import { useSessionEvents } from '@/hooks/useSessionEvents';

const mockSetSessions = vi.fn((updater: any) => {
  if (typeof updater === 'function') {
    updater([]);
  }
});
const mockSyncSession = vi.fn();

describe('useSessionEvents', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSyncSession.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'imported' });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('handles touka_request_export event', () => {
    renderHook(() => useSessionEvents([], mockSetSessions, mockSyncSession));
    
    const mockClick = vi.fn();
    const mockRemoveChild = vi.fn();
    const mockAppendChild = vi.fn();
    
    const mockLink = {
      href: '',
      download: '',
      click: mockClick,
      setAttribute: vi.fn(),
    };
    
    vi.spyOn(document, 'createElement').mockReturnValue(mockLink as any);
    vi.spyOn(document.body, 'appendChild').mockImplementation(mockAppendChild as any);
    vi.spyOn(document.body, 'removeChild').mockImplementation(mockRemoveChild as any);
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:test');

    act(() => {
      window.dispatchEvent(new CustomEvent('touka_request_export'));
    });

    expect(mockLink.download).toMatch(/^touka-export-\d+\.json$/);
    expect(mockClick).toHaveBeenCalled();
    expect(mockAppendChild).toHaveBeenCalledWith(mockLink);
    expect(mockRemoveChild).toHaveBeenCalledWith(mockLink);
  });

  it('handles touka_import_chats event with valid data', async () => {
    renderHook(() => useSessionEvents([], mockSetSessions, mockSyncSession));

    act(() => {
      window.dispatchEvent(new CustomEvent('touka_import_chats', {
        detail: [{ id: 'import-1', title: 'Imported', agent_id: 'gpt-4' }]
      }));
    });

    // Valid data with agent_id doesn't need to await pending install modal
    await waitFor(() => {
      expect(mockSetSessions).toHaveBeenCalled();
      expect(mockSyncSession).toHaveBeenCalledWith(expect.objectContaining({ id: 'import-1', title: 'Imported', agent_id: 'gpt-4' }));
      expect(toast.success).toHaveBeenCalledTimes(1);
      expect(toast.warning).not.toHaveBeenCalled();
    });
  });

  it('sanitizes sessions on export to filter auth_level and base64 attachment URLs (M2)', () => {
    const mockSessions = [
      {
        id: 'session-1',
        title: 'Session 1',
        auth_level: 'admin', // 應該被過濾
        messages: {
          messages: {
            'msg-1': {
              id: 'msg-1',
              role: 'user',
              content: 'hello',
              attachment: {
                type: 'image',
                url: 'data:image/png;base64,iVBORw0KGgoAAAANS...' // 應該被截斷
              }
            },
            'msg-2': {
              id: 'msg-2',
              role: 'assistant',
              content: 'hi',
              attachment: {
                type: 'document',
                url: 'https://example.com/file.pdf' // 不該被截斷
              }
            }
          },
          currentId: 'msg-2'
        }
      }
    ];

    renderHook(() => useSessionEvents(mockSessions as any, mockSetSessions, mockSyncSession));

    const mockClick = vi.fn();
    const mockLink = {
      href: '',
      download: '',
      click: mockClick,
    };
    
    vi.spyOn(document, 'createElement').mockReturnValue(mockLink as any);
    vi.spyOn(document.body, 'appendChild').mockImplementation(vi.fn() as any);
    vi.spyOn(document.body, 'removeChild').mockImplementation(vi.fn() as any);
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:test');

    const mockBlobConstructor = vi.fn();
    const originalBlob = globalThis.Blob;
    vi.spyOn(globalThis, 'Blob').mockImplementation(function(this: any, content: any, options: any) {
      mockBlobConstructor(content, options);
      return new originalBlob(content, options);
    } as any);

    act(() => {
      window.dispatchEvent(new CustomEvent('touka_request_export'));
    });

    expect(mockBlobConstructor).toHaveBeenCalled();
    const exportedDataStr = mockBlobConstructor.mock.calls[0][0][0];
    const exportedSessions = JSON.parse(exportedDataStr);

    expect(exportedSessions).toHaveLength(1);
    expect(exportedSessions[0].id).toBe('session-1');
    // auth_level 應被刪除
    expect(exportedSessions[0].auth_level).toBeUndefined();
    // 敏感 base64 附件網址應被截斷
    expect(exportedSessions[0].messages.messages['msg-1'].attachment.url).toBe('(image data truncated)');
    // 非 base64 附件網址應保持原樣
    expect(exportedSessions[0].messages.messages['msg-2'].attachment.url).toBe('https://example.com/file.pdf');
  });

  it('excludes incognito sessions from export (E46 / G17 / ADR-0086)', () => {
    // 既有匯出測試的 mockSessions 皆無 incognito 欄位 → useSessionEvents.ts:96 的
    // .filter(!incognito) 從不被真正驗證。含一則無痕對話 → 斷言它不進匯出。
    // 移除該 filter → 無痕對話混入匯出的備份 JSON → toEqual(['normal-1']) 紅。
    const mockSessions = [
      { id: 'normal-1', title: 'Normal', messages: { messages: {}, currentId: null } },
      { id: 'incognito-1', title: 'Secret', incognito: true, messages: { messages: {}, currentId: null } },
    ];

    renderHook(() => useSessionEvents(mockSessions as any, mockSetSessions, mockSyncSession));

    const mockLink = { href: '', download: '', click: vi.fn() };
    vi.spyOn(document, 'createElement').mockReturnValue(mockLink as any);
    vi.spyOn(document.body, 'appendChild').mockImplementation(vi.fn() as any);
    vi.spyOn(document.body, 'removeChild').mockImplementation(vi.fn() as any);
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:test');

    const mockBlobConstructor = vi.fn();
    const originalBlob = globalThis.Blob;
    vi.spyOn(globalThis, 'Blob').mockImplementation(function(this: any, content: any, options: any) {
      mockBlobConstructor(content, options);
      return new originalBlob(content, options);
    } as any);

    act(() => {
      window.dispatchEvent(new CustomEvent('touka_request_export'));
    });

    expect(mockBlobConstructor).toHaveBeenCalled();
    const exportedDataStr = mockBlobConstructor.mock.calls[0][0][0];
    const exportedSessions = JSON.parse(exportedDataStr);

    // 無痕對話永不進匯出（只留 normal-1）
    expect(exportedSessions.map((s: any) => s.id)).toEqual(['normal-1']);
    expect(exportedSessions.some((s: any) => s.id === 'incognito-1')).toBe(false);
  });
});
