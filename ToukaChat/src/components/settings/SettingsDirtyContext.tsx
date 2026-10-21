"use client";

import { createContext, useContext, useEffect, useMemo, useRef } from "react";
import type { ReactNode } from "react";

/**
 * C5 全域「未存變更」守衛：各設定表單回報自己的未存草稿狀態到中央登記簿，
 * SettingsView 於切分頁 / 瀏覽器關閉前讀取，dirty 時警告以免靜默丟失草稿。
 */
export interface SettingsDirtyStore {
    setDirty: (key: string, dirty: boolean) => void;
    isAnyDirty: () => boolean;
    clearAll: () => void;
}

const SettingsDirtyContext = createContext<SettingsDirtyStore | null>(null);

/**
 * 建立 dirty 登記簿（由 SettingsView 持有並經 Provider 下放）。以 ref 為底——
 * 表單每次按鍵更新 dirty 不觸發 re-render；守衛在切分頁/beforeunload 時即時讀取。
 */
export function useSettingsDirtyStore(): SettingsDirtyStore {
    const map = useRef<Map<string, boolean>>(new Map());
    return useMemo<SettingsDirtyStore>(
        () => ({
            setDirty: (key, dirty) => {
                if (dirty) map.current.set(key, true);
                else map.current.delete(key);
            },
            isAnyDirty: () => map.current.size > 0,
            clearAll: () => map.current.clear(),
        }),
        [],
    );
}

export function SettingsDirtyProvider({
    store,
    children,
}: {
    store: SettingsDirtyStore;
    children: ReactNode;
}) {
    return <SettingsDirtyContext.Provider value={store}>{children}</SettingsDirtyContext.Provider>;
}

/**
 * 表單一行接線：回報自己的未存狀態。`isDirty` 變化即更新；卸載時自動清除
 * （切分頁後該草稿已不存在）。無 Provider 時為 no-op（表單可獨立測試/使用）。
 */
export function useDirtyGuard(key: string, isDirty: boolean): void {
    const store = useContext(SettingsDirtyContext);
    useEffect(() => {
        if (!store) return;
        store.setDirty(key, isDirty);
        return () => store.setDirty(key, false);
    }, [store, key, isDirty]);
}
