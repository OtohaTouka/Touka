import { useCallback } from "react";

import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { logSSOTError } from "@/lib/error_handler";
import type { McpServerToolsView } from "@/lib/mcp_version";

export function useMcpTools() {
  const fetchServerTools = useCallback(
    async (key: string): Promise<McpServerToolsView | null> => {
      try {
        const response = await fetchTouka(
          getActiveProxyUrl(),
          `/v1/mcp/${encodeURIComponent(key)}/tools`,
        );
        if (response.ok) return await response.json();
      } catch (error) {
        logSSOTError(error, "useMcpTools:fetchServerTools");
      }
      return null;
    },
    [],
  );

  return { fetchServerTools };
}
