import { useState, useEffect, useCallback } from "react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { getSystemEpoch } from "@/lib/time_utils";
import { logSSOTError } from "@/lib/error_handler";

export interface ProviderTokenStats {
  prompt_tokens?: number;
  completion_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  total_cost_usd?: number;
}

// 後端 /stats 回傳形狀：agent_id -> provider -> 累計用量（SSOT）。
export type TokenUsageMap = Record<string, Record<string, ProviderTokenStats>>;

export interface DailyQuotaStats {
  daily_count: number;
  daily_max: number;
  remaining: number;
}

export interface BackgroundActivityItem {
  kind?: string;
  current?: number;
  total?: number;
  agent?: string;
}

// session_id -> 背景活動（如心跳脈衝進度）。
export type BackgroundActivityMap = Record<string, BackgroundActivityItem>;

// P5c：上次未完成的背景任務（當機/被強制關閉時殘留的 in-flight 標記）。
export interface InterruptedRun {
  session_id?: string;
  agent?: string;
  total?: number;
  started_at?: number;
}

export interface SystemStats {
  latency: string;
  connected: boolean;
  activeModels: Array<{ id: string; owned_by: string }>;
  healthStats: { cpu_usage: number; memory_usage: number } | null;
  vaultHealth: Record<string, boolean> | null;
  tokenUsage: TokenUsageMap;
  dailyQuota: DailyQuotaStats | null;
  backgroundActivity: BackgroundActivityMap;
  interruptedRuns: InterruptedRun[];
  memStats: { memory_load: number; status: string } | null;
}

export function useSystemMonitoring(isActive: boolean) {
  const [stats, setStats] = useState<SystemStats>({
    latency: "...",
    connected: false,
    activeModels: [],
    healthStats: null,
    vaultHealth: null,
    tokenUsage: {},
    dailyQuota: null,
    backgroundActivity: {},
    interruptedRuns: [],
    memStats: null,
  });

  const fetchData = useCallback(async () => {
    const proxyUrl = getActiveProxyUrl();
    const start = getSystemEpoch();

    try {
      const results = await Promise.allSettled([
        fetchTouka(proxyUrl, "/v1/health"),
        fetchTouka(proxyUrl, "/v1/models"),
        fetchTouka(proxyUrl, "/v1/system_health"),
        fetchTouka(proxyUrl, "/v1/memory/health"),
        fetchTouka(proxyUrl, "/v1/stats"),
        fetchTouka(proxyUrl, "/v1/memory/list"),
      ]);

      const lat = getSystemEpoch() - start;
      const newStats: Partial<SystemStats> = {};

      // 1. Health / Latency
      if (results[0].status === "fulfilled" && results[0].value.ok) {
        newStats.latency = `${lat}ms`;
        newStats.connected = true;
      } else {
        newStats.latency = "Error";
        newStats.connected = false;
      }

      // 2. Models
      if (results[1].status === "fulfilled" && results[1].value.ok) {
        const data = await results[1].value.json();
        newStats.activeModels = data.data || [];
      }

      // 3. System Health
      if (results[2].status === "fulfilled" && results[2].value.ok) {
        newStats.healthStats = await results[2].value.json();
      }

      // 4. Vault Health
      if (results[3].status === "fulfilled" && results[3].value.ok) {
        newStats.vaultHealth = await results[3].value.json();
      }

      // 5. Token Stats
      if (results[4].status === "fulfilled" && results[4].value.ok) {
        const data = await results[4].value.json();
        newStats.tokenUsage = data.token_usage || {};
        newStats.dailyQuota = data.daily_quota || null;
        newStats.backgroundActivity = data.background_activity || {};
        newStats.interruptedRuns = data.interrupted_runs || [];
      }

      // 6. Memory List (for count)
      if (results[5].status === "fulfilled" && results[5].value.ok) {
        const data = await results[5].value.json();
        newStats.memStats = {
          memory_load: data.memories?.length || 0,
          status: "Connected",
        };
      }

      setStats((prev) => ({ ...prev, ...newStats }));
    } catch (error) {
      logSSOTError(error, "useSystemMonitoring:fetchData");
      setStats((prev) => ({ ...prev, connected: false, latency: "Error" }));
    }
  }, []);

  useEffect(() => {
    if (!isActive) return;

    // 使用非同步方式啟動初始獲取，避免在渲染週期內同步觸發 setState (Cascading Render)
    const initFetch = async () => {
      await fetchData();
    };
    initFetch();

    const interval = setInterval(fetchData, 8000);
    return () => clearInterval(interval);
  }, [isActive, fetchData]);

  return stats;
}
