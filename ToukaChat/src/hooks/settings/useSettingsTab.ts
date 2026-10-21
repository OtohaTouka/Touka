import { useState, useEffect } from "react";
import { useLocalStorage } from "../useLocalStorage";
import { SettingsTabId, SETTINGS_TABS } from "../../components/settings/config";

const VALID_TABS = SETTINGS_TABS.map(t => t.id) as string[];

export function useSettingsTab(defaultTab: SettingsTabId = "general") {
    const [activeTab, setActiveTab] = useLocalStorage<SettingsTabId>("touka_settings_active_tab", defaultTab);
    const [isMounted, setIsMounted] = useState(false);

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- 掛載一次性：標記 mounted 並從 URL 還原 tab
        setIsMounted(true);
        const params = new URLSearchParams(window.location.search);
        const tabParam = params.get('tab');
        if (tabParam && VALID_TABS.includes(tabParam)) {
            setActiveTab(tabParam as SettingsTabId);
        }
    // setActiveTab 來自 useLocalStorage，deps 為 [key]（常數），永遠穩定 → 實質只在掛載時執行一次
    // 移除 activeTab 避免每次 tab 切換重新執行並讀到 stale URL，造成 ping-pong 無窮迴圈
    }, [setActiveTab]);

    useEffect(() => {
        if (isMounted && typeof window !== 'undefined') {
            const url = new URL(window.location.href);
            if (url.searchParams.get('tab') !== activeTab) {
                url.searchParams.set('tab', activeTab);
                window.history.replaceState(null, '', url.toString());
            }
        }
    }, [activeTab, isMounted]);

    return { activeTab, setActiveTab, isMounted };
}
