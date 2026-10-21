import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type {
  InstalledMcpConfig,
  McpPromptGetView,
  McpResourceReadView,
  McpServerPromptsView,
  McpServerResourcesView,
  McpServerResourceTemplatesView,
  McpServerToolsView,
} from "@/lib/mcp_version";
import { McpPromptsView } from "./McpPromptsView";
import { McpResourcesView } from "./McpResourcesView";
import { McpToolsView } from "./McpToolsView";

export interface McpFeatureExplorerProps {
  serverKey: string;
  serverCfg: InstalledMcpConfig[string];
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
  onSaveTools: (enabledTools: string[]) => void;
  onClose: () => void;
}

interface Catalogs {
  tools: McpServerToolsView;
  resources: McpServerResourcesView;
  templates: McpServerResourceTemplatesView;
  prompts: McpServerPromptsView;
}

export function McpFeatureExplorer({
  serverKey,
  serverCfg,
  fetchServerTools,
  fetchServerResources,
  fetchServerResourceTemplates,
  readServerResource,
  fetchServerPrompts,
  getServerPrompt,
  onSaveTools,
  onClose,
}: McpFeatureExplorerProps) {
  const { t } = useTranslation();
  const [catalogs, setCatalogs] = useState<Catalogs | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.all([
      fetchServerTools(serverKey),
      fetchServerResources(serverKey),
      fetchServerResourceTemplates(serverKey),
      fetchServerPrompts(serverKey),
    ]).then(([tools, resources, templates, prompts]) => {
      if (!alive) return;
      if (!tools || !resources || !templates || !prompts) {
        setFailed(true);
        return;
      }
      setCatalogs({ tools, resources, templates, prompts });
    });
    return () => {
      alive = false;
    };
  }, [
    fetchServerPrompts,
    fetchServerResourceTemplates,
    fetchServerResources,
    fetchServerTools,
    serverKey,
  ]);

  if (!catalogs && !failed) {
    return (
      <div className="border-border bg-muted/30 text-muted-foreground flex items-center gap-2 rounded-3xl border p-4 text-xs">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        {t("settings.mcp_features.loading")}
      </div>
    );
  }

  if (!catalogs) {
    return (
      <div className="border-destructive/30 bg-destructive/10 text-destructive flex items-center justify-between rounded-3xl border p-4 text-xs">
        {t("settings.mcp_features.fetch_failed")}
        <Button variant="ghost" size="sm" onClick={onClose}>
          {t("settings.mcp_features.close")}
        </Button>
      </div>
    );
  }

  const supportsTools = catalogs.tools.supported;
  const supportsResources =
    catalogs.resources.supported || catalogs.templates.supported;
  const supportsPrompts = catalogs.prompts.supported;
  const defaultFeature = supportsTools
    ? "tools"
    : supportsResources
      ? "resources"
      : supportsPrompts
        ? "prompts"
        : null;

  if (!defaultFeature) {
    return (
      <div className="border-border bg-muted/30 text-muted-foreground flex items-center justify-between rounded-3xl border p-4 text-xs">
        {t("settings.mcp_features.none_supported")}
        <Button variant="ghost" size="sm" onClick={onClose}>
          {t("settings.mcp_features.close")}
        </Button>
      </div>
    );
  }

  return (
    <Tabs defaultValue={defaultFeature} className="space-y-3">
      <TabsList>
        {supportsTools && (
          <TabsTrigger value="tools">
            {t("settings.mcp_features.tools")}
          </TabsTrigger>
        )}
        {supportsResources && (
          <TabsTrigger value="resources">
            {t("settings.mcp_features.resources")}
          </TabsTrigger>
        )}
        {supportsPrompts && (
          <TabsTrigger value="prompts">
            {t("settings.mcp_features.prompts")}
          </TabsTrigger>
        )}
      </TabsList>

      {supportsTools && (
        <TabsContent value="tools">
          <McpToolsView
            serverKey={serverKey}
            serverCfg={serverCfg}
            fetchServerTools={fetchServerTools}
            onSave={onSaveTools}
            onClose={onClose}
          />
        </TabsContent>
      )}
      {supportsResources && (
        <TabsContent value="resources">
          <McpResourcesView
            serverKey={serverKey}
            resources={catalogs.resources}
            templates={catalogs.templates}
            readServerResource={readServerResource}
            onClose={onClose}
          />
        </TabsContent>
      )}
      {supportsPrompts && (
        <TabsContent value="prompts">
          <McpPromptsView
            serverKey={serverKey}
            prompts={catalogs.prompts}
            getServerPrompt={getServerPrompt}
            onClose={onClose}
          />
        </TabsContent>
      )}
    </Tabs>
  );
}
