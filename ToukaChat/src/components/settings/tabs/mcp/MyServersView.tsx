import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Monitor } from "lucide-react";
import { cn } from "@/lib/utils";
import { McpRestartRequest, McpServerStatus } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { McpServerItem } from "./McpServerItem";
import { McpServerForm } from "./McpServerForm";
import { McpFeatureExplorer } from "./McpFeatureExplorer";
import { McpIsolationSection } from "./McpIsolationSection";
import { McpManualStartDialog } from "./McpManualStartDialog";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import {
  InstalledMcpConfig,
  McpPromptGetView,
  McpResourceReadView,
  McpServerPromptsView,
  McpServerResourcesView,
  McpServerResourceTemplatesView,
  McpServerToolsView,
} from "@/lib/mcp_version";

interface Props {
  mcpServers: McpServerStatus[];
  mcpConfig: InstalledMcpConfig;
  saveMcpConfig: (config: InstalledMcpConfig) => Promise<boolean>;
  fetchServerTools: (key: string) => Promise<McpServerToolsView | null>;
  fetchServerResources: (key: string) => Promise<McpServerResourcesView | null>;
  fetchServerResourceTemplates: (
    key: string,
  ) => Promise<McpServerResourceTemplatesView | null>;
  readServerResource: (
    key: string,
    uri: string,
  ) => Promise<McpResourceReadView | null>;
  fetchServerPrompts: (key: string) => Promise<McpServerPromptsView | null>;
  getServerPrompt: (
    key: string,
    name: string,
    arguments_: Record<string, string>,
  ) => Promise<McpPromptGetView | null>;
  restartMcpServer: (key: string, ack?: McpRestartRequest) => Promise<boolean>;
  restartingKeys: Set<string>;
  isSystemRefreshing: boolean;
  onRefreshList: () => void;
  editingMcpKey: string | null;
  setEditingMcpKey: (key: string | null) => void;
}

export function MyServersView({
  mcpServers,
  mcpConfig,
  saveMcpConfig,
  restartMcpServer,
  restartingKeys,
  isSystemRefreshing,
  onRefreshList,
  editingMcpKey,
  setEditingMcpKey,
  fetchServerTools,
  fetchServerResources,
  fetchServerResourceTemplates,
  readServerResource,
  fetchServerPrompts,
  getServerPrompt,
}: Props) {
  const { t } = useTranslation();
  // 展開中的工具治理面板（ADR-0195 軸 A）；與 editingMcpKey 平行、互不干擾。
  const [policyKey, setPolicyKey] = useState<string | null>(null);
  const [manualStart, setManualStart] = useState<McpServerStatus | null>(null);

  const handleSaveToolPolicy = (key: string, enabledTools: string[]) => {
    const updatedConfig = { ...mcpConfig };
    // `["*"]` ＝明示全開、`[]` ＝明示全關。**不可送 undefined/null** ——後端
    // `_preserve_masked_env` 會把它當成「客戶端沒回傳」而自磁碟還原成舊白名單。
    updatedConfig[key] = { ...updatedConfig[key], enabled_tools: enabledTools };
    saveMcpConfig(updatedConfig);
    setPolicyKey(null);
  };

  const handleToggle = (key: string) => {
    const updatedConfig = { ...mcpConfig };
    const server = updatedConfig[key];
    server.enabled = !server.enabled;
    saveMcpConfig(updatedConfig);
  };

  const { confirmToast } = useConfirmToast();

  const handleDelete = (key: string) => {
    confirmToast(
      t("settings.my_servers_view.confirm_remove_message", { key }),
      () => {
        const updatedConfig = { ...mcpConfig };
        delete updatedConfig[key];
        saveMcpConfig(updatedConfig);
      },
      t("settings.my_servers_view.confirm_remove_action"),
    );
  };

  const handleRestart = (key: string) => {
    const server = mcpServers.find((item) => item.name === key);
    if (mcpConfig[key]?.enabled === false || restartingKeys.has(key)) return;
    if (
      server?.isolation?.class === "self" &&
      server.isolation.state === "waiting_isolation" &&
      server.isolation.manual_start
    ) {
      setManualStart(server);
      return;
    }
    // 重啟會中斷該伺服器進行中的工具呼叫 → 二次確認（含警告）。
    confirmToast(
      t("settings.my_servers_view.confirm_restart_message", { key }),
      () => {
        restartMcpServer(key);
      },
      t("settings.my_servers_view.confirm_restart_action"),
    );
  };

  return (
    <div className="space-y-8">
      {manualStart && (
        <McpManualStartDialog
          server={manualStart}
          onRestart={restartMcpServer}
          onClose={() => setManualStart(null)}
        />
      )}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h4 className="text-foreground text-sm font-bold">
            {t("settings.my_servers_view.my_mcp_config_heading")}
          </h4>
          <Button
            variant="ghost"
            size="sm"
            onClick={onRefreshList}
            disabled={isSystemRefreshing}
            className="text-muted-foreground hover:text-primary group flex items-center gap-1 text-xs transition-colors disabled:opacity-50"
          >
            <Monitor
              size={12}
              className={cn(
                "group-hover:animate-pulse",
                isSystemRefreshing && "animate-spin",
              )}
            />
            {t("settings.my_servers_view.refresh_status")}
          </Button>
        </div>

        <div className="space-y-4">
          {Object.keys(mcpConfig).length > 0 ? (
            Object.keys(mcpConfig).map((key) => {
              const isRunning = mcpServers.find((s) => s.name === key);
              return (
                <div key={key} className="space-y-3">
                  <McpServerItem
                    serverKey={key}
                    serverCfg={mcpConfig[key]}
                    isRunning={isRunning}
                    onToggle={handleToggle}
                    onEdit={setEditingMcpKey}
                    onDelete={handleDelete}
                    onRestart={handleRestart}
                    onManageTools={setPolicyKey}
                    isRestarting={restartingKeys.has(key)}
                  />
                  {/* 就地展開，避免與底部的新增/編輯表單搶版位。 */}
                  {policyKey === key && (
                    <McpFeatureExplorer
                      serverKey={key}
                      serverCfg={mcpConfig[key]}
                      fetchServerTools={fetchServerTools}
                      fetchServerResources={fetchServerResources}
                      fetchServerResourceTemplates={
                        fetchServerResourceTemplates
                      }
                      readServerResource={readServerResource}
                      fetchServerPrompts={fetchServerPrompts}
                      getServerPrompt={getServerPrompt}
                      onSaveTools={(enabledTools) =>
                        handleSaveToolPolicy(key, enabledTools)
                      }
                      onClose={() => setPolicyKey(null)}
                    />
                  )}
                </div>
              );
            })
          ) : (
            <div className="border-border text-muted-foreground rounded-3xl border-2 border-dashed py-12 text-center text-xs italic">
              {t("settings.my_servers_view.empty_state")}
            </div>
          )}
        </div>
      </div>

      <McpServerForm
        mcpConfig={mcpConfig}
        editingMcpKey={editingMcpKey}
        onSave={saveMcpConfig}
        onCancel={() => setEditingMcpKey(null)}
      />
      {/* 來源與額外允許的資料夾只對已存的條目（PUT 要帶它的 bound）；存檔後依新條目重讀。 */}
      {editingMcpKey && mcpConfig[editingMcpKey] && (
        <McpIsolationSection
          key={editingMcpKey}
          serverKey={editingMcpKey}
          revision={mcpConfig[editingMcpKey]}
        />
      )}
    </div>
  );
}
