"use client";

import React, { createContext, useContext, useState, useCallback, useEffect, useRef } from "react";
import { fetchTouka, getActiveProxyUrl, setKnownAgentIds } from "../connections";
import { logSSOTError } from "@/lib/error_handler";

export interface AgentInfo {
  id: string;
  name: string;
  type: string;
  is_group: boolean;
  aliases?: string[];
}

/** 靈魂清單的狀態：一般子對話是純 ID，清單沒讀到就分不出是不是靈魂。 */
export type AgentsStatus = 'loading' | 'ready' | 'failed';

interface AgentContextType {
  agents: AgentInfo[];
  agentsStatus: AgentsStatus;
  selectedAgentId: string;
  setSelectedAgentId: (id: string) => void;
  refreshAgents: () => Promise<boolean>;
}

const AgentContext = createContext<AgentContextType | undefined>(undefined);

export function AgentProvider({ children }: { children: React.ReactNode }) {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [agentsStatus, setAgentsStatus] = useState<AgentsStatus>('loading');
  const [selectedAgentId, setSelectedAgentId] = useState<string>("");

  // ref 追蹤最新值，避免 refreshAgents useCallback 依賴 state 導致無窮重建
  const selectedAgentIdRef = useRef(selectedAgentId);
  useEffect(() => {
    selectedAgentIdRef.current = selectedAgentId;
  }, [selectedAgentId]);

  // 換了帳號或連線就加一：還在路上的舊請求不能把上一份清單蓋回來
  const scopeGeneration = useRef(0);

  const refreshAgents = useCallback(async (): Promise<boolean> => {
    const proxyUrl = getActiveProxyUrl();
    const generation = scopeGeneration.current;
    try {
      const r = await fetchTouka(proxyUrl, "/v1/agents");
      if (r.ok) {
        const data = await r.json();
        if (generation !== scopeGeneration.current) return false;
        setAgents(data);
        setAgentsStatus('ready');
        // SSOT: feed known agent ids to connections.ts for X-Touka-Soul-ID resolution
        setKnownAgentIds((data as AgentInfo[]).map((a) => a.id));
        if (data.length > 0 && !data.find((a: AgentInfo) => a.id === selectedAgentIdRef.current)) {
          setSelectedAgentId(data[0].id);
        }
        return true;
      }
    } catch (e) {
      logSSOTError(e, "AgentProvider:refreshAgents");
    }
    if (generation !== scopeGeneration.current) return false;
    // 同一個帳號讀到過就留著舊清單：一次失敗不該讓整棵樹退回猜測
    setAgentsStatus(current => current === 'ready' ? current : 'failed');
    return false;
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => {
      refreshAgents();
    }, 0);
    // ADR-0153（同族自癒）：認證狀態變動後重探。修「/login 無卡首探 401 → agents 卡 []
    // → 登入後 provider 不重掛（router.push）→ 清單要重整才出現」；連鎖影響：agents 空
    // → setKnownAgentIds 空 → 聊天不帶 X-Touka-Soul-ID（身分歸屬弱化）。換帳號重登入
    // 亦藉此重抓＝關閉跨帳號殘留清單。SessionListProvider 同款（其兄弟站點補齊）。
    const handleAuthChanged = () => {
      // 這兩個事件只在登入狀態翻轉或連線被改時發（權杖更新不會）：上一份清單不能留著分組
      scopeGeneration.current += 1;
      setAgents([]);
      setAgentsStatus('loading');
      refreshAgents();
    };
    window.addEventListener('touka_auth_changed', handleAuthChanged);
    window.addEventListener('touka_connections_updated', handleAuthChanged);
    return () => {
      clearTimeout(timer);
      window.removeEventListener('touka_auth_changed', handleAuthChanged);
      window.removeEventListener('touka_connections_updated', handleAuthChanged);
    };
  }, [refreshAgents]);

  return (
    <AgentContext.Provider value={{ agents, agentsStatus, selectedAgentId, setSelectedAgentId, refreshAgents }}>
      {children}
    </AgentContext.Provider>
  );
}

export function useAgents() {
  const context = useContext(AgentContext);
  if (!context) throw new Error("useAgents must be used within AgentProvider");
  return context;
}
