"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import {
  McpOAuthDisconnectView,
  McpOAuthStartView,
  McpOAuthStatus,
  McpOAuthStatusView,
} from "@/lib/mcp_version";

const OAUTH_POPUP_POLL_INTERVAL_MS = 1_000;
const OAUTH_POPUP_TIMEOUT_MS = 120_000;
const OAUTH_MESSAGE_TYPE = "touka:mcp-oauth";

export type McpOAuthUiError =
  | "popup_blocked"
  | "popup_timeout"
  | "request_failed";

interface OAuthMessage {
  type: typeof OAUTH_MESSAGE_TYPE;
  operationId: string;
  status: "connected" | "error";
}

interface UseMcpOAuthResult {
  status: McpOAuthStatus;
  operationId: string | null;
  expiresAt: number | null;
  authorizationServers: string[];
  busy: boolean;
  error: McpOAuthUiError | null;
  connect: (authorizationServer?: string) => Promise<boolean>;
  disconnect: () => Promise<boolean>;
  refresh: () => Promise<boolean>;
}

const TERMINAL_STATUSES = new Set<McpOAuthStatus>([
  "disabled",
  "auth_required",
  "selection_required",
  "connected",
  "frozen",
  "expired",
  "error",
]);

function isOAuthMessage(value: unknown): value is OAuthMessage {
  if (typeof value !== "object" || value === null) return false;
  const keys = Object.keys(value).sort();
  if (
    keys.length !== 3 ||
    keys[0] !== "operationId" ||
    keys[1] !== "status" ||
    keys[2] !== "type"
  ) {
    return false;
  }
  const message = value as Record<string, unknown>;
  return (
    message.type === OAUTH_MESSAGE_TYPE &&
    typeof message.operationId === "string" &&
    (message.status === "connected" || message.status === "error")
  );
}

function statusPath(serverKey: string): string {
  return `/v1/mcp/servers/${encodeURIComponent(serverKey)}/oauth/status`;
}

function oauthPath(serverKey: string): string {
  return `/v1/mcp/servers/${encodeURIComponent(serverKey)}/oauth`;
}

export function useMcpOAuth(
  serverKey: string,
  enabled: boolean,
): UseMcpOAuthResult {
  const proxyUrl = getActiveProxyUrl();
  const [snapshot, setSnapshot] = useState<McpOAuthStatusView>({
    status: enabled ? "discovering" : "disabled",
    authorization_servers: [],
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<McpOAuthUiError | null>(null);
  const runRef = useRef(0);
  const popupRef = useRef<Window | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const timeoutTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const messageListenerRef = useRef<((event: MessageEvent) => void) | null>(
    null,
  );
  const requestControllerRef = useRef<AbortController | null>(null);

  const clearInteractiveRun = useCallback((closePopup: boolean) => {
    runRef.current += 1;
    requestControllerRef.current?.abort();
    requestControllerRef.current = null;
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (timeoutTimerRef.current !== null) {
      clearTimeout(timeoutTimerRef.current);
      timeoutTimerRef.current = null;
    }
    if (messageListenerRef.current !== null) {
      window.removeEventListener("message", messageListenerRef.current);
      messageListenerRef.current = null;
    }
    if (closePopup && popupRef.current && !popupRef.current.closed) {
      popupRef.current.close();
    }
    popupRef.current = null;
  }, []);

  const requestStatus = useCallback(
    async (signal?: AbortSignal): Promise<McpOAuthStatusView | null> => {
      const response = await fetchTouka(proxyUrl, statusPath(serverKey), {
        method: "GET",
        signal,
      });
      if (!response.ok) return null;
      return (await response.json()) as McpOAuthStatusView;
    },
    [proxyUrl, serverKey],
  );

  const refresh = useCallback(async (): Promise<boolean> => {
    if (!enabled) {
      setSnapshot({ status: "disabled", authorization_servers: [] });
      return true;
    }
    const controller = new AbortController();
    requestControllerRef.current?.abort();
    requestControllerRef.current = controller;
    try {
      const next = await requestStatus(controller.signal);
      if (!next) {
        setError("request_failed");
        return false;
      }
      setSnapshot(next);
      setError(null);
      return true;
    } catch {
      if (!controller.signal.aborted) setError("request_failed");
      return false;
    } finally {
      if (requestControllerRef.current === controller) {
        requestControllerRef.current = null;
      }
    }
  }, [enabled, requestStatus]);

  const startPolling = useCallback(
    (run: number) => {
      const poll = async (): Promise<void> => {
        if (runRef.current !== run) return;
        const controller = new AbortController();
        requestControllerRef.current = controller;
        try {
          const next = await requestStatus(controller.signal);
          if (runRef.current !== run || next === null) {
            if (next === null && runRef.current === run) {
              setError("request_failed");
              setBusy(false);
            }
            return;
          }
          setSnapshot(next);
          setError(null);
          if (TERMINAL_STATUSES.has(next.status)) {
            setBusy(false);
            clearInteractiveRun(true);
            return;
          }
          pollTimerRef.current = setTimeout(
            () => void poll(),
            OAUTH_POPUP_POLL_INTERVAL_MS,
          );
        } catch {
          if (!controller.signal.aborted && runRef.current === run) {
            setError("request_failed");
            setBusy(false);
          }
        } finally {
          if (requestControllerRef.current === controller) {
            requestControllerRef.current = null;
          }
        }
      };
      void poll();
    },
    [clearInteractiveRun, requestStatus],
  );

  const connect = useCallback(
    async (authorizationServer?: string): Promise<boolean> => {
      if (!enabled) return false;
      clearInteractiveRun(true);
      const run = runRef.current;
      setBusy(true);
      setError(null);
      const controller = new AbortController();
      requestControllerRef.current = controller;
      try {
        const response = await fetchTouka(
          proxyUrl,
          `${oauthPath(serverKey)}/start`,
          {
            method: "POST",
            body: JSON.stringify({
              authorization_server: authorizationServer ?? null,
            }),
            signal: controller.signal,
          },
        );
        if (!response.ok || runRef.current !== run) {
          if (runRef.current === run) {
            setBusy(false);
            setError("request_failed");
          }
          return false;
        }
        const started = (await response.json()) as McpOAuthStartView;
        if (runRef.current !== run) return false;
        setSnapshot({
          status: "awaiting_user",
          operation_id: started.operation_id,
          expires_at: started.expires_at,
          authorization_servers: [],
        });
        if (started.authorization_url === null) {
          startPolling(run);
          return true;
        }

        const expectedOrigin = new URL(proxyUrl).origin;
        const popup = window.open(
          started.authorization_url,
          "touka-mcp-oauth",
          "popup,width=640,height=760,noopener=false",
        );
        if (popup === null) {
          setBusy(false);
          setError("popup_blocked");
          return false;
        }
        popupRef.current = popup;
        const listener = (event: MessageEvent): void => {
          if (
            runRef.current !== run ||
            event.origin !== expectedOrigin ||
            event.source !== popup ||
            !isOAuthMessage(event.data) ||
            event.data.operationId !== started.operation_id
          ) {
            return;
          }
          window.removeEventListener("message", listener);
          messageListenerRef.current = null;
          popup.close();
          popupRef.current = null;
          startPolling(run);
        };
        messageListenerRef.current = listener;
        window.addEventListener("message", listener);
        timeoutTimerRef.current = setTimeout(() => {
          if (runRef.current !== run) return;
          clearInteractiveRun(true);
          setBusy(false);
          setError("popup_timeout");
        }, OAUTH_POPUP_TIMEOUT_MS);
        return true;
      } catch {
        if (!controller.signal.aborted && runRef.current === run) {
          setBusy(false);
          setError("request_failed");
        }
        return false;
      } finally {
        if (requestControllerRef.current === controller) {
          requestControllerRef.current = null;
        }
      }
    },
    [clearInteractiveRun, enabled, proxyUrl, serverKey, startPolling],
  );

  const disconnect = useCallback(async (): Promise<boolean> => {
    if (!enabled) return false;
    clearInteractiveRun(true);
    setBusy(true);
    setError(null);
    const controller = new AbortController();
    requestControllerRef.current = controller;
    try {
      const response = await fetchTouka(proxyUrl, oauthPath(serverKey), {
        method: "DELETE",
        signal: controller.signal,
      });
      if (!response.ok) {
        setError("request_failed");
        return false;
      }
      const result = (await response.json()) as McpOAuthDisconnectView;
      setSnapshot({ status: result.status, authorization_servers: [] });
      return true;
    } catch {
      if (!controller.signal.aborted) setError("request_failed");
      return false;
    } finally {
      setBusy(false);
      if (requestControllerRef.current === controller) {
        requestControllerRef.current = null;
      }
    }
  }, [clearInteractiveRun, enabled, proxyUrl, serverKey]);

  useEffect(() => {
    clearInteractiveRun(true);
    if (!enabled) {
      setSnapshot({ status: "disabled", authorization_servers: [] });
      setBusy(false);
      setError(null);
      return;
    }
    setSnapshot({ status: "discovering", authorization_servers: [] });
    void refresh();
    return () => clearInteractiveRun(true);
  }, [clearInteractiveRun, enabled, refresh, serverKey]);

  return {
    status: snapshot.status,
    operationId: snapshot.operation_id ?? null,
    expiresAt: snapshot.expires_at ?? null,
    authorizationServers: snapshot.authorization_servers ?? [],
    busy,
    error,
    connect,
    disconnect,
    refresh,
  };
}
