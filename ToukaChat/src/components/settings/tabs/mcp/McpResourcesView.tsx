import { useState } from "react";
import { AlertTriangle, FileText, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type {
  McpResourceReadView,
  McpServerResourcesView,
  McpServerResourceTemplatesView,
} from "@/lib/mcp_version";

interface Props {
  serverKey: string;
  resources: McpServerResourcesView;
  templates: McpServerResourceTemplatesView;
  readServerResource: (
    key: string,
    uri: string,
  ) => Promise<McpResourceReadView | null>;
  onClose: () => void;
}

export function McpResourcesView({
  serverKey,
  resources,
  templates,
  readServerResource,
  onClose,
}: Props) {
  const { t } = useTranslation();
  const [result, setResult] = useState<McpResourceReadView | null>(null);
  const [loadingUri, setLoadingUri] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  const read = async (uri: string) => {
    setLoadingUri(uri);
    setFailed(false);
    const next = await readServerResource(serverKey, uri);
    setResult(next);
    setFailed(next === null);
    setLoadingUri(null);
  };

  return (
    <Card className="border-border bg-muted/30 rounded-3xl border shadow-sm">
      <CardHeader className="pb-2">
        <CardTitle className="text-muted-foreground flex items-center gap-2 text-xs font-black tracking-widest uppercase">
          <FileText size={14} className="text-primary" />
          {t("settings.mcp_features.resources_title", { name: serverKey })}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 pt-2">
        {(resources.items ?? []).length === 0 &&
          (templates.items ?? []).length === 0 && (
            <p className="text-muted-foreground text-xs">
              {t("settings.mcp_features.empty_resources")}
            </p>
          )}

        <div className="space-y-2">
          {(resources.items ?? []).map((resource) => (
            <div
              key={resource.uri}
              className="border-border bg-background/50 flex items-start justify-between gap-3 rounded-2xl border p-3"
            >
              <div className="min-w-0">
                <p className="font-mono text-xs font-bold">{resource.name}</p>
                <p className="text-muted-foreground text-xs break-all">
                  {resource.uri}
                </p>
                {resource.description && (
                  <p className="text-muted-foreground mt-1 text-xs">
                    {resource.description}
                  </p>
                )}
              </div>
              <Button
                size="sm"
                variant="outline"
                aria-label={resource.name}
                disabled={loadingUri !== null}
                onClick={() => read(resource.uri)}
              >
                {loadingUri === resource.uri && (
                  <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                )}
                {t("settings.mcp_features.read")}
              </Button>
            </div>
          ))}
        </div>

        {(templates.items ?? []).length > 0 && (
          <div className="space-y-2">
            <p className="text-muted-foreground text-xs font-bold">
              {t("settings.mcp_features.templates")}
            </p>
            {(templates.items ?? []).map((template) => (
              <div
                key={template.uri_template}
                className="border-border bg-background/30 rounded-2xl border border-dashed p-3"
              >
                <p className="font-mono text-xs font-bold">{template.name}</p>
                <p className="text-muted-foreground text-xs break-all">
                  {template.uri_template}
                </p>
              </div>
            ))}
          </div>
        )}

        {failed && (
          <p className="text-destructive text-xs">
            {t("settings.mcp_features.read_failed")}
          </p>
        )}

        {result && (
          <div className="border-border bg-background/60 space-y-2 rounded-2xl border p-3">
            {(result.result.injection_flagged ||
              result.result.dlp_redacted) && (
              <p className="text-status-warning flex items-center gap-1 text-xs">
                <AlertTriangle className="h-3.5 w-3.5" />
                {t("settings.mcp_features.content_warning")}
              </p>
            )}
            {(result.result.contents ?? []).map((content, index) =>
              content.type === "text" ? (
                <pre
                  key={`${content.uri}-${index}`}
                  className="text-xs break-words whitespace-pre-wrap"
                >
                  {content.text}
                </pre>
              ) : (
                <p
                  key={`${content.uri}-${index}`}
                  className="text-muted-foreground text-xs"
                >
                  {t("settings.mcp_features.binary_summary", {
                    mime: content.mime_type,
                    size: content.decoded_size,
                  })}
                </p>
              ),
            )}
          </div>
        )}

        <div className="flex justify-end">
          <Button variant="ghost" size="sm" onClick={onClose}>
            {t("settings.mcp_features.close")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
