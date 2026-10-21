import { useState, useEffect, useRef, useCallback } from "react";
import { SkillInstallRequest } from "../lib/types";
import { useTerminalBuffer } from "./useTerminalBuffer";
import { useLspHealth } from "./useLspHealth";
import { useAgentMessages } from "./useAgentMessages";
import { logSSOTError, errorMsg } from "@/lib/error_handler";
import { getAccessToken } from "../lib/connections";

// ── Event types (proxy → frontend) ───────────────
export type ToolName = "read_file" | "write_file" | "run_bash" | "list_dir" | "search";

export type AgentEvent =
  | { type: "tool_start";  tool: ToolName; label: string }
  | { type: "tool_done";   tool: ToolName; label: string; output: string }
  | { type: "tool_error";  tool: ToolName; label: string; error: string }
  | { type: "bash_output"; line: string; color?: string }
  | { type: "file_diff";   path: string; before: string; after: string }
  | { type: "ai_message";  text: string; model?: string }
  | { type: "lsp_status";  server: "pyright" | "eslint" | "ruff"; status: "ok" | "error" | "loading" }
  | { type: "agent_start"; sessionId: string }
  | { type: "agent_done";  sessionId: string }
  | ({ type: "skill_install_approval_request" } & SkillInstallRequest);

export type ToolStep = {
  tool: ToolName;
  label: string;
  status: "running" | "done" | "error";
  output?: string;
};

export type ChatMessage =
  | { role: "user";  text: string }
  | { role: "ai";    text: string; model?: string; steps: ToolStep[]; diff?: FileDiff }
  | { role: "agent"; steps: ToolStep[] };

export type TerminalLine = {
  text: string;
  color?: string;
};

export type FileDiff = {
  path: string;
  before: string;
  after: string;
};

export type LspStatus = {
  pyright: "ok" | "error" | "loading" | "idle";
  eslint:  "ok" | "error" | "loading" | "idle";
  ruff:    "ok" | "error" | "loading" | "idle";
};

export type UseAgentSSEOptions = {
  postUrl: string;
  streamUrl: string;
};

export type UseAgentSSEReturn = {
  messages:       ChatMessage[];
  terminalLines:  TerminalLine[];
  lspStatus:      LspStatus;
  fileDiff:       FileDiff | null;
  clearFileDiff:  () => void;
  isConnected:    boolean;
  isAgentRunning: boolean;
  sendMessage:    (text: string) => Promise<void>;
  requestLspStatus: () => void;
  pendingInstall: SkillInstallRequest | null;
  setPendingInstall: React.Dispatch<React.SetStateAction<SkillInstallRequest | null>>;
};

export function useAgentSSE({ postUrl, streamUrl }: UseAgentSSEOptions): UseAgentSSEReturn {
  const { terminalLines, handleTerminalEvent } = useTerminalBuffer();
  const { lspStatus, handleLspEvent } = useLspHealth();
  const { 
    messages, setMessages, 
    isAgentRunning, setIsAgentRunning, 
    fileDiff, clearFileDiff, 
    pendingInstall, setPendingInstall, 
    handleMessageEvent 
  } = useAgentMessages();

  const [isConnected, setIsConnected] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);

  const closeStream = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsConnected(false);
  }, []);

  const openStream = useCallback((sessionId: string) => {
    closeStream();

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    const token = getAccessToken();
    const url = `${streamUrl}?sessionId=${encodeURIComponent(sessionId)}`;

    const headers: Record<string, string> = {
      "Accept": "text/event-stream",
    };
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    setIsConnected(true);

    (async () => {
      try {
        const res = await fetch(url, { // noqa: FETCH_SSOT — SSE fetch 連線在 hook 內部定義，需自行傳入 streamUrl
          method: "GET",
          headers,
          signal: abortController.signal,
        });

        if (!res.ok) {
          throw new Error(`SSE stream connection failed: ${res.status}`);
        }

        const reader = res.body?.getReader();
        if (!reader) {
          throw new Error("No response body reader available for SSE");
        }

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            if (trimmed.startsWith("data:")) {
              const dataStr = trimmed.slice(5).trim();
              if (dataStr === "[DONE]") {
                break;
              }
              try {
                const event: AgentEvent = JSON.parse(dataStr);
                handleTerminalEvent(event);
                handleLspEvent(event);
                handleMessageEvent(event);

                if (event.type === "agent_done") {
                  closeStream();
                  return;
                }
              } catch (err) {
                logSSOTError(err, "useAgentSSE:onmessage:parse");
              }
            }
          }
        }
      } catch (err) {
        if ((err instanceof Error && err.name === "AbortError") || abortController.signal.aborted) {
          return;
        }
        logSSOTError(err, "useAgentSSE:onerror");
        closeStream();
        setIsAgentRunning(false);
      }
    })();
  }, [streamUrl, closeStream, handleTerminalEvent, handleLspEvent, handleMessageEvent, setIsAgentRunning]);

  useEffect(() => () => closeStream(), [closeStream]);

  const sendMessage = useCallback(async (text: string) => {
    if (!text.trim()) return;

    setMessages(prev => [...prev, { role: "user", text }]);

    try {
      const res = await fetch(postUrl, { // noqa: FETCH_SSOT — hook 接受呼叫方提供的完整 URL（含 IDE 外部端點），無法拆分為 fetchTouka(proxyUrl, path) 格式
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });

      if (!res.ok) {
        throw new Error(`POST ${postUrl} failed: ${res.status}`);
      }

      const { sessionId } = await res.json() as { sessionId: string };
      openStream(sessionId);
    } catch (err) {
      logSSOTError(err, "useAgentSSE:sendMessage");
      setMessages(prev => [
        ...prev,
        { role: "ai", text: errorMsg("SYSTEM_ERROR"), steps: [] },
      ]);
    }
  }, [postUrl, openStream, setMessages]);

  const requestLspStatus = useCallback(() => {
    fetch(`${postUrl}/lsp-status`).catch(() => {}); // noqa: FETCH_SSOT — fire-and-forget LSP 健康探針，無需 Auth
  }, [postUrl]);

  return {
    messages,
    terminalLines,
    lspStatus,
    fileDiff,
    clearFileDiff,
    isConnected,
    isAgentRunning,
    sendMessage,
    requestLspStatus,
    pendingInstall,
    setPendingInstall,
  };
}
