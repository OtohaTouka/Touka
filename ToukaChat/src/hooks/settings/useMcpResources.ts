import { useCallback } from "react";

import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { logSSOTError } from "@/lib/error_handler";
import type {
  McpResourceReadView,
  McpServerResourcesView,
  McpServerResourceTemplatesView,
} from "@/lib/mcp_version";

export function useMcpResources() {
  const fetchServerResources = useCallback(
    async (key: string): Promise<McpServerResourcesView | null> => {
      try {
        const response = await fetchTouka(
          getActiveProxyUrl(),
          `/v1/mcp/${encodeURIComponent(key)}/resources`,
        );
        if (response.ok) return await response.json();
      } catch (error) {
        logSSOTError(error, "useMcpResources:fetchServerResources");
      }
      return null;
    },
    [],
  );

  const fetchServerResourceTemplates = useCallback(
    async (key: string): Promise<McpServerResourceTemplatesView | null> => {
      try {
        const response = await fetchTouka(
          getActiveProxyUrl(),
          `/v1/mcp/${encodeURIComponent(key)}/resource-templates`,
        );
        if (response.ok) return await response.json();
      } catch (error) {
        logSSOTError(error, "useMcpResources:fetchServerResourceTemplates");
      }
      return null;
    },
    [],
  );

  const readServerResource = useCallback(
    async (key: string, uri: string): Promise<McpResourceReadView | null> => {
      try {
        const response = await fetchTouka(
          getActiveProxyUrl(),
          `/v1/mcp/${encodeURIComponent(key)}/resources/read`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ uri }),
          },
        );
        if (response.ok) return await response.json();
      } catch (error) {
        logSSOTError(error, "useMcpResources:readServerResource");
      }
      return null;
    },
    [],
  );

  return {
    fetchServerResources,
    fetchServerResourceTemplates,
    readServerResource,
  };
}
