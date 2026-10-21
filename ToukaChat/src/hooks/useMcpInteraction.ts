"use client";

import { useCallback } from "react";
import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";

export type McpInteractionAction = "accept" | "decline" | "cancel";

export function useMcpInteraction() {
  const resolveInteraction = useCallback(
    (
      execId: string,
      action: McpInteractionAction,
      payload?: Record<string, string | number | boolean>,
    ) => {
      const path =
        action === "accept"
          ? `/v1/tools/approve/${execId}`
          : action === "decline"
            ? `/v1/tools/reject/${execId}`
            : `/v1/tools/cancel/${execId}`;
      const options: RequestInit =
        action === "accept"
          ? {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ payload: payload || {} }),
            }
          : { method: "POST" };
      return fetchTouka(getActiveProxyUrl(), path, options);
    },
    [],
  );

  return { resolveInteraction };
}
