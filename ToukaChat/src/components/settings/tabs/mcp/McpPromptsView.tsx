import { useState } from "react";
import { AlertTriangle, Loader2, MessageSquareText } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { McpPromptGetView, McpServerPromptsView } from "@/lib/mcp_version";

interface Props {
  serverKey: string;
  prompts: McpServerPromptsView;
  getServerPrompt: (
    key: string,
    name: string,
    arguments_: Record<string, string>,
  ) => Promise<McpPromptGetView | null>;
  onClose: () => void;
}

export function McpPromptsView({
  serverKey,
  prompts,
  getServerPrompt,
  onClose,
}: Props) {
  const { t } = useTranslation();
  const [values, setValues] = useState<Record<string, Record<string, string>>>(
    {},
  );
  const [loadingName, setLoadingName] = useState<string | null>(null);
  const [result, setResult] = useState<McpPromptGetView | null>(null);
  const [failed, setFailed] = useState(false);

  const setArgument = (prompt: string, name: string, value: string) => {
    setValues((current) => ({
      ...current,
      [prompt]: { ...(current[prompt] ?? {}), [name]: value },
    }));
  };

  const preview = async (name: string) => {
    setLoadingName(name);
    setFailed(false);
    const next = await getServerPrompt(serverKey, name, values[name] ?? {});
    setResult(next);
    setFailed(next === null);
    setLoadingName(null);
  };

  const renderContent = (
    content: NonNullable<
      NonNullable<McpPromptGetView["result"]["messages"]>[number]
    >["content"],
  ) => {
    if (content.type === "text") return content.text;
    if (content.type === "resource_link") return content.uri;
    if (content.type === "resource") return content.resource.uri;
    return t("settings.mcp_features.binary_summary", {
      mime: content.mime_type,
      size: content.decoded_size,
    });
  };

  return (
    <Card className="border-border bg-muted/30 rounded-3xl border shadow-sm">
      <CardHeader className="pb-2">
        <CardTitle className="text-muted-foreground flex items-center gap-2 text-xs font-black tracking-widest uppercase">
          <MessageSquareText size={14} className="text-primary" />
          {t("settings.mcp_features.prompts_title", { name: serverKey })}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 pt-2">
        {(prompts.items ?? []).length === 0 && (
          <p className="text-muted-foreground text-xs">
            {t("settings.mcp_features.empty_prompts")}
          </p>
        )}

        {(prompts.items ?? []).map((prompt) => (
          <div
            key={prompt.name}
            className="border-border bg-background/50 space-y-2 rounded-2xl border p-3"
          >
            <div>
              <p className="font-mono text-xs font-bold">{prompt.name}</p>
              {prompt.description && (
                <p className="text-muted-foreground text-xs">
                  {prompt.description}
                </p>
              )}
            </div>
            {(prompt.arguments ?? []).map((argument) => (
              <label
                key={argument.name}
                className="text-muted-foreground block text-xs"
              >
                <span>{argument.name}</span>
                <Input
                  className="mt-1 h-8"
                  aria-label={argument.name}
                  required={argument.required}
                  value={values[prompt.name]?.[argument.name] ?? ""}
                  onChange={(event) =>
                    setArgument(prompt.name, argument.name, event.target.value)
                  }
                />
              </label>
            ))}
            <Button
              size="sm"
              variant="outline"
              aria-label={prompt.name}
              disabled={
                loadingName !== null ||
                (prompt.arguments ?? []).some(
                  (argument) =>
                    argument.required &&
                    !(values[prompt.name]?.[argument.name] ?? "").trim(),
                )
              }
              onClick={() => preview(prompt.name)}
            >
              {loadingName === prompt.name && (
                <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              )}
              {t("settings.mcp_features.preview")}
            </Button>
          </div>
        ))}

        {failed && (
          <p className="text-destructive text-xs">
            {t("settings.mcp_features.prompt_failed")}
          </p>
        )}

        {result && (
          <div className="border-status-warning/40 bg-status-warning/5 space-y-2 rounded-2xl border p-3">
            <p className="text-status-warning flex items-center gap-1 text-xs font-bold">
              <AlertTriangle className="h-3.5 w-3.5" />
              {t("settings.mcp_features.untrusted_prompt")}
            </p>
            {(result.result.messages ?? []).map((message, index) => (
              <pre
                key={`${message.role}-${index}`}
                className="text-xs break-words whitespace-pre-wrap"
              >
                {renderContent(message.content)}
              </pre>
            ))}
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
