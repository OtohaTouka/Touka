import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Globe, KeyRound } from "lucide-react";
import { McpServerStatus } from "@/lib/types";
import {
  InstalledMcpConfig,
  McpPromptGetView,
  McpResourceReadView,
  McpServerPromptsView,
  McpServerResourcesView,
  McpServerResourceTemplatesView,
  McpServerToolsView,
} from "@/lib/mcp_version";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { MyServersView } from "./mcp/MyServersView";
import { McpHubView } from "./mcp/McpHubView";
import { McpWorkloadAdminPanel } from "./mcp/McpWorkloadAdminPanel";

interface Props {
  mcpServers: McpServerStatus[];
  mcpConfig: InstalledMcpConfig;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- McpHubServer(api,可空) 與 McpHubServerView(視圖) 型別阻抗，待型別統一
  mcpHubServers: any[];
  saveMcpConfig: (config: InstalledMcpConfig) => Promise<boolean>;
  restartMcpServer: (key: string) => Promise<boolean>;
  restartingKeys: Set<string>;
  syncMcpHub: () => Promise<boolean>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 同上：Hub 視圖型別阻抗，待型別統一
  setSelectedHubServer: (server: any | null) => void;
  isSystemRefreshing: boolean;
  onRefreshList: () => void;
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
}

export function McpTab({
  mcpServers,
  mcpConfig,
  mcpHubServers,
  saveMcpConfig,
  restartMcpServer,
  restartingKeys,
  syncMcpHub,
  setSelectedHubServer,
  isSystemRefreshing,
  onRefreshList,
  fetchServerTools,
  fetchServerResources,
  fetchServerResourceTemplates,
  readServerResource,
  fetchServerPrompts,
  getServerPrompt,
}: Props) {
  const { t } = useTranslation();
  const [editingMcpKey, setEditingMcpKey] = useState<string | null>(null);

  return (
    <div className="animate-in fade-in slide-in-from-bottom-2 space-y-8 duration-300">
      <Tabs defaultValue="my_servers" className="w-full">
        <TabsList className="border-border h-auto w-full justify-start gap-6 rounded-none border-b bg-transparent p-0">
          <TabsTrigger
            value="my_servers"
            className="data-[state=active]:border-primary data-[state=active]:text-primary text-muted-foreground rounded-none border-b-2 border-transparent px-0 pt-0 pb-2 text-sm font-bold shadow-none transition-all data-[state=active]:bg-transparent"
          >
            {t("settings.mcp_tab.available_servers")}
          </TabsTrigger>
          <TabsTrigger
            value="hub"
            className="data-[state=active]:border-primary data-[state=active]:text-primary text-muted-foreground flex items-center gap-2 rounded-none border-b-2 border-transparent px-0 pt-0 pb-2 text-sm font-bold shadow-none transition-all data-[state=active]:bg-transparent"
          >
            <Globe size={14} /> {t("settings.mcp_tab.hub_explore")}
          </TabsTrigger>
          <TabsTrigger
            value="workload_oauth"
            className="data-[state=active]:border-primary data-[state=active]:text-primary text-muted-foreground flex items-center gap-2 rounded-none border-b-2 border-transparent px-0 pt-0 pb-2 text-sm font-bold shadow-none transition-all data-[state=active]:bg-transparent"
          >
            <KeyRound size={14} /> {t("settings.mcp_tab.workload_oauth")}
          </TabsTrigger>
        </TabsList>

        <div className="pt-6">
          <TabsContent
            value="my_servers"
            className="m-0 focus-visible:outline-none"
          >
            <MyServersView
              mcpServers={mcpServers}
              mcpConfig={mcpConfig}
              saveMcpConfig={saveMcpConfig}
              restartMcpServer={restartMcpServer}
              restartingKeys={restartingKeys}
              isSystemRefreshing={isSystemRefreshing}
              onRefreshList={onRefreshList}
              editingMcpKey={editingMcpKey}
              setEditingMcpKey={setEditingMcpKey}
              fetchServerTools={fetchServerTools}
              fetchServerResources={fetchServerResources}
              fetchServerResourceTemplates={fetchServerResourceTemplates}
              readServerResource={readServerResource}
              fetchServerPrompts={fetchServerPrompts}
              getServerPrompt={getServerPrompt}
            />
          </TabsContent>

          <TabsContent value="hub" className="m-0 focus-visible:outline-none">
            <McpHubView
              mcpConfig={mcpConfig}
              mcpHubServers={mcpHubServers}
              syncMcpHub={syncMcpHub}
              setSelectedHubServer={setSelectedHubServer}
            />
          </TabsContent>
          <TabsContent
            value="workload_oauth"
            className="m-0 focus-visible:outline-none"
          >
            <McpWorkloadAdminPanel />
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
}
