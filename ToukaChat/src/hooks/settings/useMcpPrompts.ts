import { useCallback } from "react";

import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { logSSOTError } from "@/lib/error_handler";
import type { McpPromptGetView, McpServerPromptsView } from "@/lib/mcp_version";

export function useMcpPrompts() {
  const fetchServerPrompts = useCallback(
    async (key: string): Promise<McpServerPromptsView | null> => {
      try {
        const response = await fetchTouka(
          getActiveProxyUrl(),
          `/v1/mcp/${encodeURIComponent(key)}/prompts`,
        );
        if (response.ok) return await response.json();
      } catch (error) {
        logSSOTError(error, "useMcpPrompts:fetchServerPrompts");
      }
      return null;
    },
    [],
  );

  const getServerPrompt = useCallback(
    async (
      key: string,
      name: string,
      arguments_: Record<string, string>,
    ): Promise<McpPromptGetView | null> => {
      try {
        const response = await fetchTouka(
          getActiveProxyUrl(),
          `/v1/mcp/${encodeURIComponent(key)}/prompts/get`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name,
              arguments: Object.keys(arguments_).length > 0 ? arguments_ : null,
            }),
          },
        );
        if (response.ok) return await response.json();
      } catch (error) {
        logSSOTError(error, "useMcpPrompts:getServerPrompt");
      }
      return null;
    },
    [],
  );

  return { fetchServerPrompts, getServerPrompt };
}
