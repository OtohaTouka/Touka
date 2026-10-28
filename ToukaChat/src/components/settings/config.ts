import { Settings2, Cable, Monitor, Wrench, Box, Zap, MessageSquare, Shield, Settings, LucideIcon, Users, Palette, History, KeyRound, Webhook } from "lucide-react";

export type SettingsTabId = "general" | "theme" | "connections" | "models" | "tools" | "mcp" | "skills" | "vscode" | "security" | "sns_bots" | "env" | "group" | "users" | "sliding_window" | "provider_keys" | "hooks";

/** 設定頁 IA 分組（側邊欄以此分類標題聚合分頁，減少 15 分頁平鋪蔓延）。 */
export type SettingsGroupId = "personal" | "models" | "capabilities" | "conversation" | "system";

export interface SettingsTabConfig {
    id: SettingsTabId;
    /** i18n key（消費端 SettingsView 以 t(label) 渲染） */
    label: string;
    icon: LucideIcon;
    /** i18n key，用於主內容區域的標題（消費端以 t(title) 渲染） */
    title: string;
    /** IA 分組（側邊欄分類標題用）。 */
    group: SettingsGroupId;
    /**
     * C1：admin/系統專屬分頁。後端各端點已 RBAC 把關（安全邊界在後端），此旗標
     * 讓 SettingsView 對非 admin（人類 role !== 'admin'）隱藏分頁、避免 false-affordance
     * 與徒勞的 403 抓取。未設＝所有已認證使用者皆可見（general 個人設定、theme 純前端）。
     */
    adminOnly?: boolean;
}

export interface SettingsGroupConfig {
    id: SettingsGroupId;
    /** i18n key（分類標題）。 */
    label: string;
}

/** 分組顯示順序（SSOT）：個人 → 模型 → 能力 → 對話 → 系統管理。 */
export const SETTINGS_GROUPS: SettingsGroupConfig[] = [
    { id: "personal", label: "settings.groups.personal" },
    { id: "models", label: "settings.groups.models" },
    { id: "capabilities", label: "settings.groups.capabilities" },
    { id: "conversation", label: "settings.groups.conversation" },
    { id: "system", label: "settings.groups.system" },
];

// 註冊表：在此處新增或修改分頁，完全符合開放封閉原則 (OCP)。陣列順序＝組內顯示順序。
// label/title 存 i18n key（語意化），實際文案在 src/locales/<lng>/translation.json。
// adminOnly：除 general（自助個資/密碼）與 theme（純前端外觀）外，其餘皆為系統/管理設定。
export const SETTINGS_TABS: SettingsTabConfig[] = [
    { id: "general", label: "settings.tabs.general.label", title: "settings.tabs.general.title", icon: Settings2, group: "personal" },
    { id: "theme", label: "settings.tabs.theme.label", title: "settings.tabs.theme.title", icon: Palette, group: "personal" },
    { id: "models", label: "settings.tabs.models.label", title: "settings.tabs.models.title", icon: Monitor, group: "models", adminOnly: true },
    { id: "provider_keys", label: "settings.tabs.provider_keys.label", title: "settings.tabs.provider_keys.title", icon: KeyRound, group: "models", adminOnly: true },
    { id: "connections", label: "settings.tabs.connections.label", title: "settings.tabs.connections.title", icon: Cable, group: "models", adminOnly: true },
    { id: "tools", label: "settings.tabs.tools.label", title: "settings.tabs.tools.title", icon: Wrench, group: "capabilities", adminOnly: true },
    { id: "mcp", label: "settings.tabs.mcp.label", title: "settings.tabs.mcp.title", icon: Box, group: "capabilities", adminOnly: true },
    { id: "skills", label: "settings.tabs.skills.label", title: "settings.tabs.skills.title", icon: Zap, group: "capabilities", adminOnly: true },
    { id: "group", label: "settings.tabs.group.label", title: "settings.tabs.group.title", icon: MessageSquare, group: "conversation", adminOnly: true },
    { id: "sliding_window", label: "settings.tabs.sliding_window.label", title: "settings.tabs.sliding_window.title", icon: History, group: "conversation", adminOnly: true },
    { id: "sns_bots", label: "settings.tabs.sns_bots.label", title: "settings.tabs.sns_bots.title", icon: MessageSquare, group: "conversation", adminOnly: true },
    { id: "users", label: "settings.tabs.users.label", title: "settings.tabs.users.title", icon: Users, group: "system", adminOnly: true },
    { id: "security", label: "settings.tabs.security.label", title: "settings.tabs.security.title", icon: Shield, group: "system", adminOnly: true },
    { id: "hooks", label: "settings.tabs.hooks.label", title: "settings.tabs.hooks.title", icon: Webhook, group: "system", adminOnly: true },
    { id: "vscode", label: "settings.tabs.vscode.label", title: "settings.tabs.vscode.title", icon: Monitor, group: "system", adminOnly: true },
    { id: "env", label: "settings.tabs.env.label", title: "settings.tabs.env.title", icon: Settings, group: "system", adminOnly: true },
];

/**
 * C1：依角色過濾可見分頁（SSOT）。非 admin 僅見非 adminOnly 分頁（general / theme）。
 * 後端仍是安全邊界；此處僅為前端 affordance 與避免徒勞 403。
 */
export function visibleSettingsTabs(isAdmin: boolean): SettingsTabConfig[] {
    return SETTINGS_TABS.filter((tab) => !tab.adminOnly || isAdmin);
}

/**
 * 依分組順序聚合可見分頁；略過無可見分頁的組（如非 admin 僅見「個人」組）。
 * 供側邊欄渲染分類標題＋其下分頁（IA 重整，減少分頁蔓延）。
 */
export function groupedVisibleSettingsTabs(
    isAdmin: boolean,
): { group: SettingsGroupConfig; tabs: SettingsTabConfig[] }[] {
    const visible = visibleSettingsTabs(isAdmin);
    return SETTINGS_GROUPS
        .map((group) => ({ group, tabs: visible.filter((tab) => tab.group === group.id) }))
        .filter((entry) => entry.tabs.length > 0);
}
