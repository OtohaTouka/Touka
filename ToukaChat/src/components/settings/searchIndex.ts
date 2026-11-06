import { SettingsTabId, SETTINGS_TABS, visibleSettingsTabs } from "@/components/settings/config";

/**
 * 全域設定搜尋的策展索引（IA 路線圖）。每筆＝一個可搜尋的設定項：
 *  - `label`：i18n key（結果清單顯示）。
 *  - `keywords`：**內嵌雙語同義詞**，僅供比對、永不顯示（故無需 i18n；中英並列使任一語言皆可命中）。
 * 新增設定＝索引加一筆（OCP）。比對範圍＝本地化 label ＋ keywords ＋ 所屬分頁名。
 */
export interface SettingsSearchEntry {
    id: string;
    tabId: SettingsTabId;
    label: string;
    keywords: string;
}

export const SETTINGS_SEARCH_INDEX: SettingsSearchEntry[] = [
    // 個人
    { id: "profile", tabId: "general", label: "settings.search.entries.profile", keywords: "profile 個人資料 顯示名稱 display name 暱稱 簡介 bio avatar 頭像" },
    { id: "password", tabId: "general", label: "settings.search.entries.password", keywords: "password 密碼 改密碼 變更密碼 change password 登入" },
    { id: "clear_data", tabId: "general", label: "settings.search.entries.clear_data", keywords: "clear data 清空 清除 我的資料 wipe reset delete 刪除" },
    { id: "export_data", tabId: "general", label: "settings.search.entries.export_data", keywords: "export gdpr 匯出 資料 download 下載 可攜 portability" },
    // 外觀
    { id: "theme_mode", tabId: "theme", label: "settings.search.entries.theme_mode", keywords: "theme dark light mode 主題 亮 暗 深色 淺色 跟隨系統 外觀 appearance" },
    { id: "accent", tabId: "theme", label: "settings.search.entries.accent", keywords: "accent color 強調色 顏色 主題色 colour" },
    { id: "font_size", tabId: "theme", label: "settings.search.entries.font_size", keywords: "font size 字級 字體 文字大小 typography" },
    // 模型與供應商
    { id: "model_toggle", tabId: "models", label: "settings.search.entries.model_toggle", keywords: "model enable visible 模型 啟用 開啟 隱藏 可見 visibility" },
    { id: "model_health", tabId: "models", label: "settings.search.entries.model_health", keywords: "health test 健康 測試 連線 latency 延遲" },
    { id: "provider_keys", tabId: "provider_keys", label: "settings.search.entries.provider_keys", keywords: "api key 金鑰 openai anthropic gemini grok deepseek provider 供應商 credentials" },
    { id: "connections", tabId: "connections", label: "settings.search.entries.connections", keywords: "connection 連線 base url endpoint 端點 timeout 逾時 proxy" },
    // 代理人能力
    { id: "tools", tabId: "tools", label: "settings.search.entries.tools", keywords: "tool 工具 native capability 能力" },
    { id: "mcp", tabId: "mcp", label: "settings.search.entries.mcp", keywords: "mcp server hub install 安裝 伺服器 model context protocol" },
    { id: "skills", tabId: "skills", label: "settings.search.entries.skills", keywords: "skill 技能 catalog 目錄 persona 角色 prompt" },
    // 對話
    { id: "group_whitelist", tabId: "group", label: "settings.search.entries.group_whitelist", keywords: "group chat 群聊 whitelist 白名單 alias 別名 mention @ 多代理人" },
    { id: "sliding_window", tabId: "sliding_window", label: "settings.search.entries.sliding_window", keywords: "sliding window context 上下文 視窗 記憶 摘要 summary 保留輪數 trim" },
    { id: "sns_bots", tabId: "sns_bots", label: "settings.search.entries.sns_bots", keywords: "bot discord telegram 機器人 sns 平台 platform" },
    // 系統管理
    { id: "rbac", tabId: "security", label: "settings.search.entries.rbac", keywords: "permission rbac 權限 role 角色 安全 security matrix 矩陣" },
    { id: "identity_map", tabId: "security", label: "settings.search.entries.identity_map", keywords: "identity mapping 身分 映射 capability 能力" },
    { id: "autonomy", tabId: "security", label: "settings.search.entries.autonomy", keywords: "autonomy budget 自主 預算 quota 配額 cost 花費 cap 上限" },
    { id: "hooks", tabId: "hooks", label: "settings.search.entries.hooks", keywords: "hook hooks 鉤子 事件 event 攔截 block 擋下 PreToolUse PostToolUse UserPromptSubmit Stop webhook 自動化 automation" },
    { id: "users", tabId: "users", label: "settings.search.entries.users", keywords: "user 使用者 帳號 account role 角色 admin 管理員 invite 邀請" },
    { id: "agent_reset", tabId: "users", label: "settings.search.entries.agent_reset", keywords: "reset 重置 agent 代理人 清除資料 clear data wipe" },
    { id: "backup", tabId: "env", label: "settings.search.entries.backup", keywords: "backup restore 備份 還原 download 下載 encrypt 加密" },
    { id: "backup_schedule", tabId: "env", label: "settings.search.entries.backup_schedule", keywords: "schedule 排程 自動備份 auto backup 保留 retention" },
    { id: "config_snapshot", tabId: "env", label: "settings.search.entries.config_snapshot", keywords: "config snapshot 設定快照 匯入 匯出 import export 可攜 portable" },
];

type TFn = (key: string) => string;

/**
 * 依輸入字串搜尋設定項（純函式、可單測）。比對本地化 label ＋ keywords ＋ 分頁名；
 * 僅回傳當前角色可見分頁的項目；以 label 命中優先排序（label 子字串 > 起始 > 僅 keyword）。
 * 多 token 採 AND（每個 token 都須命中）。
 */
export function searchSettings(query: string, t: TFn, isAdmin: boolean): SettingsSearchEntry[] {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    const tokens = q.split(/\s+/).filter(Boolean);
    const visible = new Set(visibleSettingsTabs(isAdmin).map((tab) => tab.id));
    const tabLabel = new Map(SETTINGS_TABS.map((tab) => [tab.id, t(tab.label).toLowerCase()]));

    const scored = SETTINGS_SEARCH_INDEX
        .filter((entry) => visible.has(entry.tabId))
        .map((entry) => {
            const label = t(entry.label).toLowerCase();
            const hay = `${label} ${entry.keywords.toLowerCase()} ${tabLabel.get(entry.tabId) ?? ""}`;
            if (!tokens.every((tok) => hay.includes(tok))) return { entry, score: 0 };
            const score = (label.startsWith(q) ? 100 : 0) + (label.includes(q) ? 50 : 0) + 10;
            return { entry, score };
        })
        .filter((x) => x.score > 0);

    scored.sort((a, b) => b.score - a.score);
    return scored.map((x) => x.entry);
}
