"use client";

import React, { createContext, useContext, useState, useCallback, useEffect, useMemo } from "react";
import { fetchTouka, getActiveProxyUrl } from "../connections";
import { logSSOTError } from "@/lib/error_handler";
import { pickDefaultModel } from "@/lib/default_model";

export interface ModelInfo {
  id: string;
  name: string;
  owned_by: string;
  object?: string;
  is_agent?: boolean;
  enabled?: boolean;
  visibility?: 'visible' | 'hidden';
  status?: 'online' | 'offline';
  // ADR-0111：原生模型 per-model 預設取樣參數（null＝未設定→回退預設）＋供應商能力旗標
  // （皆由後端自 Provider Registry SSOT 衍生；前端不硬編供應商知識）。
  temperature?: number | null;
  top_p?: number | null;
  top_k?: number | null;
  params_editable?: boolean;
  supports_top_k?: boolean;
  temperature_max?: number;
}

/** ADR-0111：可寫入 /models/{id}/settings 的設定（null＝清除回退預設；未送＝不變）。 */
export interface ModelSettingsPatch {
  enabled?: boolean;
  visibility?: 'visible' | 'hidden';
  temperature?: number | null;
  top_p?: number | null;
  top_k?: number | null;
}

/** POST /v1/models/{id}/health 的回傳（200 body）。error 為已消毒的穩定機器碼。 */
export interface ModelHealthResult {
  status: string; // 'healthy' | 'degraded' | 'unavailable'
  latency_ms?: number;
  error?: string; // 穩定碼：timeout / connection_error / request_failed / auth_missing / api_error:{n} / not_supported / not_found
}

interface ModelContextType {
  allModels: ModelInfo[];
  availableModels: ModelInfo[];
  selectedModel: string;
  setSelectedModel: (model: string) => void;
  refreshModels: () => Promise<void>;
  triggerBackendRefresh: () => Promise<void>;
  isRefreshing: boolean;
  updateModelSetting: (id: string, settings: ModelSettingsPatch) => Promise<boolean>;
  deleteModel: (id: string) => Promise<void>;
  testModel: (id: string) => Promise<ModelHealthResult>;
}

const ModelContext = createContext<ModelContextType | undefined>(undefined);

export function ModelProvider({ children }: { children: React.ReactNode }) {
  const [allModels, setAllModels] = useState<ModelInfo[]>([]);
  const [isRefreshing, setIsRefreshing] = useState(false);
  // 不寫死模型名：清單載入後若還沒選，選第一個可用的一般模型（ADR-0250）。
  const [selectedModel, setSelectedModel] = useState<string>("");

  // 可用模型（聊天選單來源）為 allModels 的衍生狀態（SSOT：唯一事實來源是 allModels）。
  // 規則：已開啟 且（非隱藏 或 為當前選中模型）。包含離線模型避免紀錄消失；
  // 保留當前選中模型即使被隱藏，避免使用者隱藏正在使用的模型後選單變空白。
  const availableModels = useMemo(
    () => allModels.filter((m: ModelInfo) =>
      m.enabled && ((m.visibility ?? 'visible') !== 'hidden' || m.id === selectedModel)
    ),
    [allModels, selectedModel]
  );

  const refreshModels = useCallback(async () => {
    const proxyUrl = getActiveProxyUrl();
    try {
      const r = await fetchTouka(proxyUrl, "/v1/models");
      const d = await r.json();
      const rawModels: ModelInfo[] = d?.data || [];

      // 注意：後端傳回的名稱可能與 id 不同 (例如 ToukaSoulAlice)
      // 我們保留原始資料並排序
      const sortedModels = [...rawModels].sort((a,b) => a.id.localeCompare(b.id));
      setAllModels(sortedModels);
      setSelectedModel(prev => prev || (pickDefaultModel(sortedModels)?.id ?? ""));
    } catch (e) {
      logSSOTError(e, "ModelProvider:refreshModels");
    }
  }, []);

  const triggerBackendRefresh = useCallback(async () => {
    const proxyUrl = getActiveProxyUrl();
    setIsRefreshing(true);
    try {
      const res = await fetchTouka(proxyUrl, "/v1/models/refresh", { method: 'POST' });
      if (!res.ok) {
        logSSOTError(new Error(`/models/refresh ${res.status}`), "ModelProvider:triggerBackendRefresh");
      }
    } catch (e) {
      logSSOTError(e, "ModelProvider:triggerBackendRefresh");
    } finally {
      await refreshModels();
      setIsRefreshing(false);
    }
  }, [refreshModels]);

  const updateModelSetting = useCallback(async (id: string, settings: ModelSettingsPatch): Promise<boolean> => {
    const proxyUrl = getActiveProxyUrl();
    try {
      const res = await fetchTouka(proxyUrl, `/v1/models/${id}/settings`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings)
      });
      if (res.ok) {
        await refreshModels();
        return true;
      }
      return false;
    } catch (e) {
      logSSOTError(e, "ModelProvider:updateModelSetting");
      return false;
    }
  }, [refreshModels]);

  const deleteModel = useCallback(async (id: string) => {
    const proxyUrl = getActiveProxyUrl();
    const res = await fetchTouka(proxyUrl, `/v1/models/${id}`, { method: 'DELETE' });
    if (!res.ok) {
      const body = await res.json().catch(() => ({})) as Record<string, unknown>;
      const detail = body?.detail as Record<string, unknown> | undefined;
      const msg = (detail?.message as string | undefined) ?? `Delete failed (${res.status})`;
      throw new Error(msg);
    }
    await refreshModels();
  }, [refreshModels]);

  // 主動健康探測：呼叫既有 /health 端點並回傳已消毒結果。
  // 嚴禁觸發 refreshModels —— 探測不得改動模型設定（Model Governance / SSOT）。
  const testModel = useCallback(async (id: string): Promise<ModelHealthResult> => {
    const proxyUrl = getActiveProxyUrl();
    try {
      const res = await fetchTouka(proxyUrl, `/v1/models/${id}/health`, { method: 'POST' });
      if (!res.ok) return { status: 'unavailable', error: 'request_failed' };
      return await res.json() as ModelHealthResult;
    } catch (e) {
      logSSOTError(e, "ModelProvider:testModel");
      return { status: 'unavailable', error: 'request_failed' };
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => {
      refreshModels();
    }, 0);

    const handleRefresh = () => refreshModels();
    window.addEventListener("touka_refresh_models", handleRefresh);
    // ADR-0153（同族自癒）：認證狀態變動後重探。修「/login 無卡首探 401 → 模型選單空
    // → 登入後不重抓、要重整才出現」；換帳號重登入亦重抓＝關閉跨帳號殘留清單。
    const handleAuthChanged = () => { refreshModels(); };
    window.addEventListener("touka_auth_changed", handleAuthChanged);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("touka_refresh_models", handleRefresh);
      window.removeEventListener("touka_auth_changed", handleAuthChanged);
    };
  }, [refreshModels]);

  return (
    <ModelContext.Provider value={{ 
      allModels, 
      availableModels, 
      selectedModel, 
      setSelectedModel, 
      refreshModels,
      triggerBackendRefresh,
      isRefreshing,
      updateModelSetting,
      deleteModel,
      testModel
    }}>
      {children}
    </ModelContext.Provider>
  );
}

export function useModels() {
  const context = useContext(ModelContext);
  if (!context) throw new Error("useModels must be used within ModelProvider");
  return context;
}
