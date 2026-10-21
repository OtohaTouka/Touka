import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import {
  errorMsg,
  getErrorMessage,
  getResponseDetailString,
  logSSOTError,
} from "@/lib/error_handler";
import {
  MCP_ENTRY_CHANGED,
  mcpIsolationErrorMessage,
} from "@/lib/mcp_isolation";
import type {
  McpIsolationUpdateRequest,
  McpIsolationView,
  SandboxPathGrant,
} from "@/lib/types";

type IsolationChange = Omit<McpIsolationUpdateRequest, "bound">;

const isolationPath = (serverKey: string) =>
  `/v1/mcp/${encodeURIComponent(serverKey)}/isolation`;

/**
 * 一台 MCP 的來源與額外允許的資料夾（`GET`／`PUT /mcp/{name}/isolation`）。
 *
 * PUT 一律帶 GET 拿到的 `bound`（條目的程式身分），只送這次改的欄位；後端只寫
 * 隔離設定檔，下次重新啟動那台才生效。`revision` 改變（例如存檔改了程式）時重讀。
 */
export function useMcpIsolation(serverKey: string, revision?: unknown) {
  const { t } = useTranslation();
  const [view, setView] = useState<McpIsolationView | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const failureMessage = useCallback(
    async (res: Response, code: string | undefined) => {
      const known = mcpIsolationErrorMessage(t, code);
      if (known) return known;
      // 合約驗證失敗（detail 是清單）：這個畫面送的只有資料夾，多半是路徑不完整
      if (res.status === 422 && code === undefined) {
        return t("settings.mcp_isolation.errors.invalid_paths");
      }
      return getErrorMessage(res, "CONFIG_SAVE_FAILED");
    },
    [t],
  );

  const load = useCallback(async () => {
    try {
      const res = await fetchTouka(
        getActiveProxyUrl(),
        isolationPath(serverKey),
      );
      if (res.ok) {
        setView((await res.json()) as McpIsolationView);
        setError(null);
        return;
      }
      const code = await getResponseDetailString(res, "message");
      setError(await failureMessage(res, code));
    } catch (e) {
      logSSOTError(e, "useMcpIsolation:load");
      setError(errorMsg("NETWORK_CONNECTION_ERROR"));
    }
  }, [serverKey, failureMessage]);

  useEffect(() => {
    void load();
  }, [load, revision]);

  const update = async (change: IsolationChange): Promise<boolean> => {
    if (!view) return false;
    const body: McpIsolationUpdateRequest = { bound: view.bound, ...change };
    setSaving(true);
    try {
      const res = await fetchTouka(
        getActiveProxyUrl(),
        isolationPath(serverKey),
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (res.ok) {
        setView((await res.json()) as McpIsolationView);
        setError(null);
        toast.success(t("settings.mcp_isolation.saved"));
        return true;
      }
      const code = await getResponseDetailString(res, "message");
      // 條目在讀取之後被改過：先讀新的 bound，人重新確認後再送
      if (code === MCP_ENTRY_CHANGED) await load();
      setError(await failureMessage(res, code));
    } catch (e) {
      logSSOTError(e, "useMcpIsolation:update");
      setError(errorMsg("NETWORK_CONNECTION_ERROR"));
    } finally {
      setSaving(false);
    }
    return false;
  };

  return {
    view,
    saving,
    error,
    markAgent: () => update({ origin: "agent" }),
    saveSandboxPaths: (sandbox_paths: SandboxPathGrant[]) =>
      update({ sandbox_paths }),
  };
}
