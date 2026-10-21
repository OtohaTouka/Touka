import { useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { McpConfigSaveResponse, McpRestartRequest, McpServerStatus } from "@/lib/types";
import { InstalledMcpConfig, McpHubServerView } from "@/lib/mcp_version";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { logSSOTError, errorMsg, getResponseDetailString } from "@/lib/error_handler";
import { mcpIsolationErrorMessage } from "@/lib/mcp_isolation";
import { toast } from "sonner";
import { useMcpPrompts } from "./useMcpPrompts";
import { useMcpResources } from "./useMcpResources";
import { useMcpTools } from "./useMcpTools";

export function useMcpSettings() {
  const { t } = useTranslation();
  const { fetchServerTools } = useMcpTools();
  const {
    fetchServerResources,
    fetchServerResourceTemplates,
    readServerResource,
  } = useMcpResources();
  const { fetchServerPrompts, getServerPrompt } = useMcpPrompts();
  const [mcpServers, setMcpServers] = useState<McpServerStatus[]>([]);
  // 形狀 SSOT＝InstalledMcpConfig（含 remote 的 url/headers，ADR-0192）；就地重刻會與
  // computeMcpStatus／buildInstalledEntry 的消費形狀脫鉤。
  const [mcpConfig, setMcpConfig] = useState<InstalledMcpConfig>({});
  const [mcpHubServers, setMcpHubServers] = useState<McpHubServerView[]>([]);
  // 進行中的重啟（防重複送出：請求未回前停用該台重啟鈕）。
  const [restartingKeys, setRestartingKeys] = useState<Set<string>>(new Set());

  const fetchMcpData = useCallback(async () => {
    const proxyUrl = getActiveProxyUrl();
    try {
      const [mcpRes, hubRes, mcpConfRes] = await Promise.allSettled([
        fetchTouka(proxyUrl, "/v1/mcp/list"),
        fetchTouka(proxyUrl, "/v1/mcp/hub"),
        fetchTouka(proxyUrl, "/v1/mcp/ui/config"),
      ]);
      if (mcpRes.status === "fulfilled" && mcpRes.value.ok)
        setMcpServers((await mcpRes.value.json()).servers || []);
      if (hubRes.status === "fulfilled" && hubRes.value.ok)
        setMcpHubServers((await hubRes.value.json()).hub || []);
      if (mcpConfRes.status === "fulfilled" && mcpConfRes.value.ok)
        setMcpConfig((await mcpConfRes.value.json()).mcpServers || {});
    } catch (e) {
      logSSOTError(e, "useMcpSettings:fetchMcpData");
    }
  }, []);

  const fetchMcpListOnly = async () => {
    const proxyUrl = getActiveProxyUrl();
    try {
      const mcpRes = await fetchTouka(proxyUrl, "/v1/mcp/list");
      if (mcpRes.ok) setMcpServers((await mcpRes.json()).servers || []);
    } catch (e) {
      logSSOTError(e, "useMcpSettings:fetchMcpListOnly");
    }
  };

  const restartMcpServer = async (key: string, ack?: McpRestartRequest) => {
    const proxyUrl = getActiveProxyUrl();
    setRestartingKeys((prev) => new Set(prev).add(key));
    try {
      const res = await fetchTouka(
        proxyUrl,
        `/v1/mcp/${encodeURIComponent(key)}/restart`,
        {
          method: "POST",
          ...(ack
            ? {
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(ack),
              }
            : {}),
        },
      );
      if (res.ok) {
        const data = await res.json().catch(() => ({ status: "restarted" }));
        await fetchMcpListOnly();
        if (data.status === "restarted") {
          toast.success(t("hooks.use_mcp_settings.restart_success", { key }));
          return true;
        }
        if (data.status === "busy") {
          toast.warning(t("hooks.use_mcp_settings.restart_busy", { key }));
          return false;
        }
        if (data.status === "waiting") {
          toast.warning(t("hooks.use_mcp_settings.restart_waiting", { key }));
          return false;
        }
      } else {
        const message = mcpIsolationErrorMessage(
          t,
          await getResponseDetailString(res, "message"),
        );
        if (message) {
          await fetchMcpListOnly();
          toast.error(message);
          return false;
        }
      }
    } catch (e) {
      logSSOTError(e, "useMcpSettings:restartMcpServer");
    } finally {
      setRestartingKeys((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
    toast.error(t("hooks.use_mcp_settings.restart_failed", { key }));
    return false;
  };

  const saveMcpConfig = async (newConfig: InstalledMcpConfig) => {
    const proxyUrl = getActiveProxyUrl();
    try {
      const res = await fetchTouka(proxyUrl, "/v1/mcp/ui/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mcpServers: newConfig }),
      });
      if (res.ok) {
        // 形狀 SSOT＝InstalledMcpConfig：此處原本就地重刻一個窄型別，已因此漏掉
        // url/headers；新增欄位時再重刻一次就是第三次漂移。
        setMcpConfig(newConfig);
        // 程式改了的伺服器，人給的額外允許資料夾已被後端清掉（不能套到另一個程式上）。
        const saved = (await res.json().catch(() => ({}))) as Partial<McpConfigSaveResponse>;
        const cleared = saved.isolation_cleared ?? [];
        if (cleared.length > 0)
          toast.warning(t("hooks.use_mcp_settings.isolation_cleared", { names: cleared.join(", ") }));
        await fetchMcpListOnly();
        return true;
      }
    } catch (_err) {
      logSSOTError(_err, "useMcpSettings:saveMcpConfig");
    }
    // 集中失敗回饋（P2）：非-ok 與 catch 皆落到此處，所有呼叫端（toggle/delete/add/install）統一有錯誤提示。
    toast.error(errorMsg("CONFIG_SAVE_FAILED"));
    return false;
  };

  const syncMcpHub = async () => {
    const proxyUrl = getActiveProxyUrl();
    try {
      const res = await fetchTouka(proxyUrl, "/v1/mcp/hub/sync", {
        method: "POST",
      });
      if (res.ok) {
        const hubRes = await fetchTouka(proxyUrl, "/v1/mcp/hub");
        if (hubRes.ok) setMcpHubServers((await hubRes.json()).hub || []);
        return true;
      }
    } catch (e) {
      logSSOTError(e, "useMcpSettings:syncMcpHub");
    }
    toast.error(errorMsg("CONFIG_SAVE_FAILED"));
    return false;
  };

  return {
    mcpServers,
    mcpConfig,
    mcpHubServers,
    restartingKeys,
    fetchMcpData,
    fetchMcpListOnly,
    saveMcpConfig,
    syncMcpHub,
    restartMcpServer,
    fetchServerTools,
    fetchServerResources,
    fetchServerResourceTemplates,
    readServerResource,
    fetchServerPrompts,
    getServerPrompt,
  };
}
