// src/components/settings/SettingsView.tsx
"use client";

import { useState, useEffect, useCallback, useMemo, Fragment } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { McpInstallModal } from "@/components/modals/McpInstallModal";
import { useModels } from "@/lib/providers/ModelProvider";
import { buildInstalledEntry, hubEntryConfigKey } from "@/lib/mcp_version";
import { McpHubServer } from "@/lib/types";
import { EnvSettings } from "@/components/dashboard/EnvSettings";
import {
  SETTINGS_TABS,
  SettingsTabId,
  visibleSettingsTabs,
  groupedVisibleSettingsTabs,
} from "@/components/settings/config";
import { searchSettings } from "@/components/settings/searchIndex";
import { useAuth } from "@/contexts/AuthContext";
import { useSettingsTab } from "@/hooks/settings/useSettingsTab";
import { useLocalStorage } from "@/hooks/useLocalStorage";
import { useConnectionSettings } from "@/hooks/settings/useConnectionSettings";
import { useSnsBotSettings } from "@/hooks/settings/useSnsBotSettings";
import { useSecuritySettings } from "@/hooks/settings/useSecuritySettings";
import { ConnectionsTab } from "@/components/settings/tabs/ConnectionsTab";
import { GeneralTab } from "@/components/settings/tabs/GeneralTab";
import { ModelsTab } from "@/components/settings/tabs/ModelsTab";
import { ToolsTab } from "@/components/settings/tabs/ToolsTab";
import { SkillsTab } from "@/components/settings/tabs/SkillsTab";
import { McpTab } from "@/components/settings/tabs/McpTab";
import { SecurityTab } from "@/components/settings/tabs/SecurityTab";
import { GroupSettingsTab } from "@/components/settings/tabs/GroupSettingsTab";
import { ThemeSettingsTab } from "@/components/settings/tabs/ThemeSettingsTab";
import { UsersTab } from "@/components/settings/tabs/UsersTab";
import { useToolSettings } from "@/hooks/settings/useToolSettings";
import { useSkillSettings } from "@/hooks/settings/useSkillSettings";
import { useMcpSettings } from "@/hooks/settings/useMcpSettings";
import { useUserSettings } from "@/hooks/settings/useUserSettings";
import { SlidingWindowTab } from "@/components/settings/tabs/SlidingWindowTab";
import { HooksTab } from "@/components/settings/tabs/HooksTab";
import { ProviderKeysTab } from "@/components/settings/tabs/ProviderKeysTab";
import { SnsBotsTab } from "@/components/settings/tabs/SnsBotsTab";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { AlertTriangle, Search, ChevronDown } from "lucide-react";
import {
  useSettingsDirtyStore,
  SettingsDirtyProvider,
} from "@/components/settings/SettingsDirtyContext";
import { cn } from "@/lib/utils";

export function SettingsView() {
  const { t } = useTranslation();
  const { activeTab, setActiveTab, isMounted } = useSettingsTab();
  const { triggerBackendRefresh } = useModels();
  const { user } = useAuth();

  // C1：人類管理員角色為 'admin'（JWT 身分）。非 admin 僅見 general / theme。
  const isAdmin = user?.role === "admin";
  const visibleTabs = useMemo(() => visibleSettingsTabs(isAdmin), [isAdmin]);
  // IA：側邊欄依分組（分類標題）聚合分頁，減少 15 分頁平鋪蔓延。
  const groupedTabs = useMemo(
    () => groupedVisibleSettingsTabs(isAdmin),
    [isAdmin],
  );
  // C3：渲染期就把越權/失效分頁視為 general，避免 effect 回退前閃現 admin 內容（縱深防禦）。
  const effectiveTab = visibleTabs.some((tab) => tab.id === activeTab)
    ? activeTab
    : "general";

  // C5：全域未存變更守衛 —— 各表單經 useDirtyGuard 回報 dirty；切分頁/關閉前若有未存草稿則警告。
  const dirty = useSettingsDirtyStore();
  const [pendingTab, setPendingTab] = useState<SettingsTabId | null>(null);

  // 全域設定搜尋（跨分頁打字找設定）：查詢非空時側邊欄改顯結果清單，點選跳至該分頁。
  const [query, setQuery] = useState("");
  const searchResults = useMemo(
    () => searchSettings(query, t, isAdmin),
    [query, t, isAdmin],
  );
  const tabMeta = useMemo(
    () => new Map(SETTINGS_TABS.map((tab) => [tab.id, tab])),
    [],
  );
  const isSearching = query.trim().length > 0;

  // 群組摺疊（桌機側邊欄；只展開目前群組）：記住手動展開的群組，
  // 但目前所在分頁的群組永遠可見（force-show）。手機維持橫向平鋪、不套用摺疊。
  const [expandedGroupIds, setExpandedGroupIds] = useLocalStorage<string[]>(
    "touka_settings_expanded_groups",
    [],
  );
  const expandedSet = useMemo(
    () => new Set(expandedGroupIds),
    [expandedGroupIds],
  );
  const activeGroupId = useMemo(
    () =>
      groupedTabs.find(({ tabs }) =>
        tabs.some((tab) => tab.id === effectiveTab),
      )?.group.id,
    [groupedTabs, effectiveTab],
  );
  const isGroupShown = (groupId: string) =>
    groupId === activeGroupId || expandedSet.has(groupId);
  const toggleGroup = (groupId: string) => {
    setExpandedGroupIds((prev) =>
      prev.includes(groupId)
        ? prev.filter((g) => g !== groupId)
        : [...prev, groupId],
    );
  };

  const requestTabChange = (val: string) => {
    if (dirty.isAnyDirty()) setPendingTab(val as SettingsTabId);
    else setActiveTab(val as SettingsTabId);
  };
  const pickSearchResult = (tabId: SettingsTabId) => {
    requestTabChange(tabId);
    setQuery("");
  };
  const confirmDiscard = () => {
    if (pendingTab) {
      dirty.clearAll();
      setActiveTab(pendingTab);
    }
    setPendingTab(null);
  };

  // 瀏覽器關閉/重整：dirty 時觸發原生離開警告（涵蓋分頁切換守衛之外的離開路徑）。
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirty.isAnyDirty()) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  const { fetchConnections } = useConnectionSettings(triggerBackendRefresh);

  const [isSystemRefreshing, setIsSystemRefreshing] = useState(false);

  // --- Tools & MCP & Skills State ---
  const { systemTools, fetchTools } = useToolSettings();
  const {
    mcpServers,
    mcpConfig,
    mcpHubServers,
    restartingKeys,
    fetchMcpData,
    fetchMcpListOnly,
    saveMcpConfig,
    syncMcpHub,
    restartMcpServer,
    fetchServerTools,
    fetchServerResources,
    fetchServerResourceTemplates,
    readServerResource,
    fetchServerPrompts,
    getServerPrompt,
  } = useMcpSettings();
  const {
    skills,
    editingSkill,
    isSavingSkill,
    setEditingSkill,
    fetchSkills,
    fetchSkillCatalog,
    saveSkill,
    deleteSkill,
    exportSkill,
    installSkillFromUrl,
    catalogItems,
    installCatalogSkill,
    getSkill,
  } = useSkillSettings();

  const [selectedHubServer, setSelectedHubServer] =
    useState<McpHubServer | null>(null);

  // --- Security Settings State ---
  const {
    permissions,
    setPermissions,
    identityMap,
    setIdentityMap,
    allAgents,
    autonomyConfig,
    setAutonomyConfig,
    isSavingPermissions,
    isSavingIdentity,
    isSavingAutonomy,
    fetchSecurityData,
    savePermissions,
    saveIdentity,
    saveAutonomy,
  } = useSecuritySettings();

  // --- SNS Bots State ---
  const snsSettings = useSnsBotSettings();
  const { fetchSnsBots } = snsSettings;

  // --- User Management State ---
  const { fetchUsers } = useUserSettings();

  const fetchSystemInfo = useCallback(async () => {
    const tasks = [
      fetchConnections(),
      fetchSnsBots(),
      fetchSecurityData(),
      fetchTools(),
      fetchMcpData(),
      fetchSkills(),
      fetchSkillCatalog(),
      fetchUsers(),
    ];

    try {
      setIsSystemRefreshing(true);
      await Promise.allSettled(tasks);
    } finally {
      setIsSystemRefreshing(false);
    }
  }, [
    fetchConnections,
    fetchSnsBots,
    fetchSecurityData,
    fetchTools,
    fetchMcpData,
    fetchSkills,
    fetchSkillCatalog,
    fetchUsers,
  ]);

  // --- Persistence & Initialization ---
  // C2：所有系統資料抓取皆 admin-only 端點；非 admin 不觸發（避免徒勞 403 與浪費請求）。
  useEffect(() => {
    if (!isMounted || !isAdmin) return;
    fetchSystemInfo();
  }, [isMounted, isAdmin, fetchSystemInfo]);

  // C3：URL ?tab= / localStorage 還原的分頁若非當前角色可見（如非 admin 深連 ?tab=users），
  // 退回 general，避免落在隱藏分頁的空白內容。
  useEffect(() => {
    if (!isMounted) return;
    if (!visibleTabs.some((tab) => tab.id === activeTab)) {
      // 修正越權/失效分頁的一次性回退（條件式回退，非無條件 setState）。
      setActiveTab("general");
    }
  }, [isMounted, activeTab, visibleTabs, setActiveTab]);

  const handleInstallHubServer = async (envVars: Record<string, string>) => {
    if (!selectedHubServer) return;
    const updatedConfig = { ...mcpConfig };
    // 寫入鍵＝徽章查表鍵（hubEntryConfigKey SSOT，ADR-0192）：舊碼以 hub.id 寫入、
    // 徽章卻查 server_key，同一服務落成兩個鍵——徽章永遠不亮，且下面「保留既有
    // env 密鑰」因取不到舊條目而失效。組裝邏輯（含 stdio/remote 分形）已抽為純函式。
    const configKey = hubEntryConfigKey(selectedHubServer);
    updatedConfig[configKey] = buildInstalledEntry(
      selectedHubServer,
      envVars,
      mcpConfig[configKey],
    );
    // P2：依存檔結果分支——成功才關 modal＋報成功；失敗保留 modal 供重試
    //（錯誤 toast 由 saveMcpConfig 集中跳 CONFIG_SAVE_FAILED，避免雙重提示與假成功）。
    const ok = await saveMcpConfig(updatedConfig);
    if (ok) {
      setSelectedHubServer(null);
      toast.success(t("settings.view.install_success"));
    }
  };

  return (
    <SettingsDirtyProvider store={dirty}>
      <div className="bg-background flex h-full w-full flex-col overflow-hidden md:flex-row">
        <Tabs
          value={effectiveTab}
          onValueChange={requestTabChange}
          className="flex h-full w-full flex-col md:flex-row"
          orientation="vertical"
        >
          {/* Sidebar / Topbar */}
          <div className="border-border bg-sidebar flex shrink-0 flex-col border-b md:w-56 md:border-r md:border-b-0">
            <div className="hidden p-4 pb-2 md:block md:p-6 md:pb-4">
              <h2 className="text-muted-foreground text-xs font-black tracking-widest uppercase">
                {t("settings.settings")}
              </h2>
            </div>

            {/* 全域設定搜尋（桌機側邊欄；跨分頁打字找設定）。 */}
            <div className="hidden px-2 pt-1 pb-1 md:block">
              <div className="relative">
                <Search className="text-muted-foreground absolute top-1/2 left-2.5 h-4 w-4 -translate-y-1/2" />
                <input
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && searchResults.length > 0)
                      pickSearchResult(searchResults[0].tabId);
                  }}
                  placeholder={t("settings.search.placeholder")}
                  aria-label={t("settings.search.placeholder")}
                  className="bg-muted/40 border-input focus:ring-primary/40 w-full rounded-xl border py-2 pr-3 pl-9 text-sm transition-all outline-none focus:ring-2"
                />
              </div>
            </div>

            <TabsList
              aria-label={t("settings.tabs_aria")}
              className={cn(
                "custom-scrollbar flex h-auto w-full flex-row justify-start gap-1 overflow-x-auto rounded-none border-0 bg-transparent p-2 md:w-auto md:flex-col md:items-stretch md:overflow-y-auto",
                isSearching && "hidden",
              )}
            >
              {groupedTabs.map(({ group, tabs }) => {
                const shown = isGroupShown(group.id);
                return (
                  <Fragment key={group.id}>
                    {/* 分類標題＝摺疊開關：僅桌機垂直側邊欄顯示（手機橫向捲動維持平鋪、不摺疊）。
                                    目前所在群組永遠展開（force-show）。 */}
                    <button
                      type="button"
                      onClick={() => toggleGroup(group.id)}
                      aria-expanded={shown}
                      className="text-muted-foreground hover:text-foreground hidden w-full items-center justify-between gap-2 px-2.5 pt-3 pb-1 text-xs font-black tracking-widest uppercase transition-colors select-none md:flex"
                    >
                      <span>{t(group.label)}</span>
                      <ChevronDown
                        aria-hidden
                        className={cn(
                          "h-3.5 w-3.5 shrink-0 transition-transform",
                          !shown && "-rotate-90",
                        )}
                      />
                    </button>
                    {tabs.map((tab) => (
                      <TabsTrigger
                        key={tab.id}
                        value={tab.id}
                        className={cn(
                          "data-[state=active]:bg-primary data-[state=active]:text-primary-foreground text-muted-foreground hover:bg-muted/50 hover:text-foreground flex w-max shrink-0 items-center justify-start gap-3 rounded-xl p-2.5 text-sm font-bold transition-all data-[state=active]:scale-100 data-[state=active]:shadow-sm md:w-full",
                          // 摺疊只作用於桌機；手機（橫向平鋪、無標題）一律顯示。
                          !shown && "md:hidden",
                        )}
                      >
                        <tab.icon className="h-4 w-4 shrink-0" />
                        <span className="truncate whitespace-nowrap">
                          {t(tab.label)}
                        </span>
                      </TabsTrigger>
                    ))}
                  </Fragment>
                );
              })}
            </TabsList>

            {/* 搜尋結果（跨分頁；點選跳至該分頁，走 C5 未存守衛）。 */}
            {isSearching && (
              <div
                className="custom-scrollbar flex flex-col gap-1 overflow-y-auto p-2"
                role="list"
                aria-label={t("settings.search.results_aria")}
              >
                {searchResults.length === 0 ? (
                  <p className="text-muted-foreground px-2.5 py-4 text-xs italic">
                    {t("settings.search.no_results")}
                  </p>
                ) : (
                  searchResults.map((entry) => {
                    const meta = tabMeta.get(entry.tabId);
                    const Icon = meta?.icon;
                    return (
                      <button
                        key={entry.id}
                        onClick={() => pickSearchResult(entry.tabId)}
                        className="text-muted-foreground hover:bg-muted/50 hover:text-foreground flex w-full items-center justify-start gap-3 rounded-xl p-2.5 text-left text-sm font-bold transition-all"
                      >
                        {Icon && <Icon className="h-4 w-4 shrink-0" />}
                        <span className="flex min-w-0 flex-col">
                          <span className="truncate">{t(entry.label)}</span>
                          <span className="text-muted-foreground truncate text-xs font-medium">
                            {t("settings.search.in_tab", {
                              tab: meta ? t(meta.label) : "",
                            })}
                          </span>
                        </span>
                      </button>
                    );
                  })
                )}
              </div>
            )}
          </div>

          {/* Main Content Area */}
          <div className="bg-background relative flex h-full min-w-0 flex-1 flex-col overflow-hidden">
            {/* Header */}
            <div className="border-border bg-background z-10 flex shrink-0 flex-col border-b p-4 pb-2 md:p-6 md:pb-4">
              <h3 className="text-foreground text-lg font-black tracking-tight uppercase md:text-xl">
                {t(
                  SETTINGS_TABS.find((tab) => tab.id === effectiveTab)?.title ||
                    "settings.settings",
                )}
              </h3>
            </div>

            {/* Scrollable Content */}
            <div className="custom-scrollbar relative flex-1 overflow-y-auto p-4 md:p-6 md:px-10">
              <div className="mx-auto max-w-7xl space-y-8 pb-20">
                {/* --- General Tab --- */}
                <TabsContent value="general" className="mt-0 outline-none">
                  <GeneralTab />
                </TabsContent>

                {/* --- Theme Tab --- */}
                <TabsContent value="theme" className="mt-0 outline-none">
                  <ThemeSettingsTab />
                </TabsContent>

                {/* --- Connections Tab --- */}
                <TabsContent value="connections" className="mt-0 outline-none">
                  <ConnectionsTab
                    isSystemRefreshing={isSystemRefreshing}
                    onRefresh={fetchSystemInfo}
                  />
                </TabsContent>

                {/* --- Models Tab --- */}
                <TabsContent value="models" className="mt-0 outline-none">
                  <ModelsTab />
                </TabsContent>

                <TabsContent value="group" className="mt-0 outline-none">
                  <GroupSettingsTab />
                </TabsContent>

                {/* --- Users Tab --- */}
                <TabsContent value="users" className="mt-0 outline-none">
                  <UsersTab />
                </TabsContent>

                {/* --- Tools Tab --- */}
                <TabsContent value="tools" className="mt-0 outline-none">
                  <ToolsTab systemTools={systemTools} />
                </TabsContent>

                {/* --- MCP Tab --- */}
                <TabsContent value="mcp" className="mt-0 outline-none">
                  <McpTab
                    mcpServers={mcpServers}
                    mcpConfig={mcpConfig}
                    mcpHubServers={mcpHubServers}
                    saveMcpConfig={saveMcpConfig}
                    restartMcpServer={restartMcpServer}
                    restartingKeys={restartingKeys}
                    syncMcpHub={syncMcpHub}
                    setSelectedHubServer={setSelectedHubServer}
                    isSystemRefreshing={isSystemRefreshing}
                    onRefreshList={fetchMcpListOnly}
                    fetchServerTools={fetchServerTools}
                    fetchServerResources={fetchServerResources}
                    fetchServerResourceTemplates={fetchServerResourceTemplates}
                    readServerResource={readServerResource}
                    fetchServerPrompts={fetchServerPrompts}
                    getServerPrompt={getServerPrompt}
                  />
                </TabsContent>

                {/* --- Skills Tab --- */}
                <TabsContent value="skills" className="mt-0 outline-none">
                  <SkillsTab
                    skills={skills}
                    editingSkill={editingSkill}
                    isSavingSkill={isSavingSkill}
                    setEditingSkill={setEditingSkill}
                    saveSkill={saveSkill}
                    deleteSkill={deleteSkill}
                    exportSkill={exportSkill}
                    installSkillFromUrl={installSkillFromUrl}
                    catalogItems={catalogItems}
                    installCatalogSkill={installCatalogSkill}
                    getSkill={getSkill}
                    systemTools={systemTools}
                  />
                </TabsContent>

                {/* --- Security Tab --- */}
                <TabsContent value="security" className="mt-0 outline-none">
                  <SecurityTab
                    permissions={permissions}
                    setPermissions={setPermissions}
                    identityMap={identityMap}
                    setIdentityMap={setIdentityMap}
                    allAgents={allAgents}
                    autonomyConfig={autonomyConfig}
                    setAutonomyConfig={setAutonomyConfig}
                    isSavingPermissions={isSavingPermissions}
                    isSavingIdentity={isSavingIdentity}
                    isSavingAutonomy={isSavingAutonomy}
                    savePermissions={savePermissions}
                    saveIdentity={saveIdentity}
                    saveAutonomy={saveAutonomy}
                    systemTools={systemTools}
                    mcpServers={mcpServers}
                    skills={skills}
                  />
                </TabsContent>

                {/* --- Hooks Tab（ADR-0292）--- */}
                <TabsContent value="hooks" className="mt-0 outline-none">
                  <HooksTab />
                </TabsContent>

                {/* --- Sliding Window Tab --- */}
                <TabsContent
                  value="sliding_window"
                  className="mt-0 outline-none"
                >
                  <SlidingWindowTab />
                </TabsContent>

                {/* --- Provider API Keys Tab --- */}
                <TabsContent
                  value="provider_keys"
                  className="mt-0 outline-none"
                >
                  <ProviderKeysTab />
                </TabsContent>

                {/* --- SNS Bots Tab --- */}
                <TabsContent value="sns_bots" className="mt-0 outline-none">
                  <SnsBotsTab allAgents={allAgents} settings={snsSettings} />
                </TabsContent>

                {/* --- Env Tab --- */}
                <TabsContent value="env" className="mt-0 outline-none">
                  <div className="animate-in fade-in slide-in-from-bottom-2 space-y-8 duration-300">
                    <EnvSettings />
                  </div>
                </TabsContent>
              </div>
            </div>
          </div>
        </Tabs>

        {/* MCP Install Modal */}
        <McpInstallModal
          isOpen={selectedHubServer !== null}
          server={selectedHubServer}
          onClose={() => setSelectedHubServer(null)}
          onConfirm={handleInstallHubServer}
        />

        {/* C5：未存變更確認對話框（切分頁守衛） */}
        <Dialog
          open={pendingTab !== null}
          onOpenChange={(o) => {
            if (!o) setPendingTab(null);
          }}
        >
          <DialogContent
            overlayClassName="bg-background/60 backdrop-blur-md"
            className="bg-card border-border w-full max-w-md space-y-5 rounded-3xl border p-8 shadow-2xl"
          >
            <div className="text-destructive flex items-center gap-3">
              <AlertTriangle className="h-6 w-6 shrink-0" />
              <DialogTitle className="text-foreground text-lg font-black">
                {t("settings.unsaved.title")}
              </DialogTitle>
            </div>
            <DialogDescription className="text-foreground/80 text-sm leading-relaxed">
              {t("settings.unsaved.body")}
            </DialogDescription>
            <div className="grid grid-cols-2 gap-3 pt-1">
              <Button
                variant="outline"
                onClick={() => setPendingTab(null)}
                className="h-10 rounded-xl text-xs font-bold"
              >
                {t("settings.unsaved.keep_editing")}
              </Button>
              <Button
                variant="destructive"
                onClick={confirmDiscard}
                className="h-10 rounded-xl text-xs font-bold"
              >
                {t("settings.unsaved.discard")}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>
    </SettingsDirtyProvider>
  );
}
