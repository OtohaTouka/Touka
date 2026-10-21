/**
 * useSettingsTab — URL 參數初始化與 Tab 切換穩定性
 *
 * 驗證：
 * 1. 掛載時正確讀取 URL ?tab=X 並設定 activeTab
 * 2. Tab 切換後 activeTab 不因 stale URL 被強制回退（ping-pong 無窮迴圈修復）
 * 3. URL 無 tab 參數時，使用傳入的預設值
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

// ── Mock 必須在 import 之前 ──────────────────────────────────────

vi.mock('@/components/settings/config', () => ({
    SETTINGS_TABS: [
        { id: 'general',     label: 'General',     icon: null, title: 'General' },
        { id: 'security',    label: 'Security',    icon: null, title: 'Security' },
        { id: 'connections', label: 'Connections', icon: null, title: 'Connections' },
    ],
}));

vi.mock('@/lib/error_handler', () => ({
    logSSOTError: vi.fn(),
}));

// 繞開 Node.js 26 native localStorage（--localstorage-file 缺失），
// 以 React.useState 取代 useLocalStorage，使 hook 在 jsdom 外可純粹測試
vi.mock('../useLocalStorage', async () => {
    const React = await import('react');
    return {
        useLocalStorage: vi.fn(
            <T>(_key: string, initialValue: T) => React.useState<T>(initialValue)
        ),
    };
});

import { useSettingsTab } from '../settings/useSettingsTab';

// ── 工具函數：設定 window.location mock ────────────────────────

function setMockLocation(search: string) {
    Object.defineProperty(window, 'location', {
        configurable: true,
        value: {
            search,
            href: `http://localhost/${search}`,
        },
    });
}

// ── 測試套件 ───────────────────────────────────────────────────

describe('useSettingsTab — URL 參數與 Tab 切換穩定性', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        // replaceState 不更新 window.location，模擬 Effect 2 尚未更新 URL 的競爭情境
        vi.spyOn(window.history, 'replaceState').mockImplementation(() => {});
        setMockLocation('');
    });

    afterEach(() => {
        vi.restoreAllMocks();
        setMockLocation('');
    });

    it('掛載時從 URL ?tab=security 正確初始化 activeTab（覆寫預設值 "general"）', async () => {
        setMockLocation('?tab=security');

        const { result } = renderHook(() => useSettingsTab('general'));

        await waitFor(() => expect(result.current.isMounted).toBe(true));

        // URL 中的 tab 參數應覆寫預設值 "general"
        expect(result.current.activeTab).toBe('security');
    });

    it('URL 含 ?tab=general 時，切換到 security 後 activeTab 不被回退（無 ping-pong 迴圈）', async () => {
        // 模擬用戶已在 general tab，URL 有 ?tab=general
        setMockLocation('?tab=general');

        const { result } = renderHook(() => useSettingsTab('general'));
        await waitFor(() => expect(result.current.isMounted).toBe(true));
        expect(result.current.activeTab).toBe('general');

        // 用戶點擊 "security" tab
        act(() => {
            result.current.setActiveTab('security');
        });

        // 等待所有 effect 執行完畢（含可能的連鎖更新）
        await new Promise(r => setTimeout(r, 50));

        // 修復後：activeTab 穩定在 "security"
        // 修復前：Effect 1 依賴 activeTab，讀到 stale URL ?tab=general，
        //         呼叫 setActiveTab("general") 將用戶選擇強制回退
        expect(result.current.activeTab).toBe('security');
    });

    it('掛載時 URL 無 ?tab 參數，activeTab 使用傳入的預設值', async () => {
        setMockLocation('');

        const { result } = renderHook(() => useSettingsTab('connections'));

        await waitFor(() => expect(result.current.isMounted).toBe(true));

        expect(result.current.activeTab).toBe('connections');
    });

    it('掛載後 isMounted 變為 true（useEffect 正確執行）', async () => {
        const { result } = renderHook(() => useSettingsTab('general'));

        await waitFor(() => expect(result.current.isMounted).toBe(true));

        expect(result.current.isMounted).toBe(true);
    });
});
