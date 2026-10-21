// src/components/dashboard/HeartbeatWhitelist.tsx
"use client";

import { useState, useEffect, useCallback } from "react";
import { 
    ShieldCheck, Trash2, Crown, Lock, 
    AlertCircle, RefreshCw, UserPlus 
} from "lucide-react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { cn } from "@/lib/utils";
import { IdentityMap, AgentIdentity } from "@/lib/types";
import { logSSOTError } from "@/lib/error_handler";
import { useTranslation } from "react-i18next";

const HEARTBEAT_CAP = "heartbeat_awakening";

export function HeartbeatWhitelist() {
    const { t } = useTranslation();
    const [allModels, setAllModels] = useState<AgentIdentity[]>([]);
    const [selectedAgentId, setSelectedAgentId] = useState("");
    const [isLoading, setIsLoading] = useState(false);
    const [isUpdating, setIsUpdating] = useState(false);

    const [identityMap, setIdentityMap] = useState<IdentityMap | null>(null);

    // 後端只列能有代理角色的 ID（靈魂、DebugAgent、DispatchAgent；ADR-0245），並附上含類型
    // 規則的有效能力——靈魂經類型規則取得的心跳權限也算在名單內。
    const whitelist = allModels
        .filter(m => m.effective_capabilities?.includes(HEARTBEAT_CAP))
        .map(m => m.id);

    const fetchModels = useCallback(async () => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/identity/agents");
            if (res.ok) {
                const data = await res.json();
                setAllModels(data || []);
            }
        } catch (e) {
            logSSOTError(e, "HeartbeatWhitelist:fetchAgents");
        }
    }, []);

    const fetchWhitelist = useCallback(async () => {
        setIsLoading(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/identity");
            if (res.ok) setIdentityMap(await res.json());
        } catch (e) {
            logSSOTError(e, "HeartbeatWhitelist:fetchIdentityMap");
        }
        setIsLoading(false);
    }, []);

    // 覆寫以有效角色與能力為底：沒有覆寫的靈魂原本是 Owner＋類型規則的能力，
    // 不可寫成 { role: "Base" } 把它降級（ADR-0245）。
    const setHeartbeat = async (agentId: string, grant: boolean) => {
        const agent = allModels.find(m => m.id === agentId);
        if (!identityMap || !agent) return;
        setIsUpdating(true);
        const base = identityMap.agent_overrides[agentId] ?? {
            role: agent.effective_role ?? identityMap.default_role,
            capabilities: agent.effective_capabilities ?? [],
        };
        const others = base.capabilities.filter(cap => cap !== HEARTBEAT_CAP);
        const newMap: IdentityMap = {
            ...identityMap,
            agent_overrides: {
                ...identityMap.agent_overrides,
                [agentId]: { ...base, capabilities: grant ? [...others, HEARTBEAT_CAP] : others },
            },
        };
        try {
            const res = await fetchTouka(getActiveProxyUrl(), "/v1/identity", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(newMap)
            });
            if (res.ok) {
                await Promise.all([fetchWhitelist(), fetchModels()]);
                if (grant) setSelectedAgentId("");
            }
        } catch (e) {
            logSSOTError(e, grant ? "HeartbeatWhitelist:handleAdd" : "HeartbeatWhitelist:handleRemove");
        }
        setIsUpdating(false);
    };

    const handleAdd = () => setHeartbeat(selectedAgentId, true);
    const handleRemove = (agentId: string) => setHeartbeat(agentId, false);

    useEffect(() => {
        // 使用 setTimeout 確保 state 更新發生在 Effect 提交階段之後，避免 Cascading renders 警告
        const timer = setTimeout(() => {
            fetchWhitelist();
            fetchModels();
        }, 0);
        return () => clearTimeout(timer);
    }, [fetchWhitelist, fetchModels]);

    const isSoul = (_id: string) => false;  // Soul detection delegated to backend

    return (
        <div className="border-border bg-sidebar space-y-6 rounded-3xl border p-6 shadow-lg">
            <div className="flex items-center justify-between px-2">
                <h3 className="flex items-center gap-2 text-base font-black tracking-tight uppercase italic text-foreground">
                    <ShieldCheck className="h-5 w-5 text-status-success" /> {t("dashboard.heartbeat_whitelist.title")}
                </h3>
                <button 
                    onClick={() => { fetchWhitelist(); fetchModels(); }}
                    className="hover:bg-background hover:text-primary rounded-full p-2 text-muted-foreground transition-colors"
                    disabled={isLoading}
                >
                    <RefreshCw className={cn("w-4 h-4", isLoading && "animate-spin")} />
                </button>
            </div>

            {/* Add Section */}
            <div className="border-border bg-background flex flex-col gap-3 rounded-2xl border p-4 sm:flex-row">
                <div className="flex-1 space-y-1">
                    <label className="px-1 text-xs font-black text-muted-foreground uppercase">{t("dashboard.heartbeat_whitelist.select_label")}</label>
                    <select 
                        value={selectedAgentId}
                        onChange={e => setSelectedAgentId(e.target.value)}
                        className="border-input bg-sidebar focus:ring-primary w-full rounded-xl border px-4 py-2 text-sm outline-none focus:ring-2"
                    >
                        <option value="">{t("dashboard.heartbeat_whitelist.select_placeholder")}</option>
                        {allModels
                            .filter(m => !whitelist.includes(m.id))
                            .map(m => (
                                <option key={m.id} value={m.id}>{m.name || m.id}</option>
                            ))
                        }
                    </select>
                </div>
                <div className="flex items-end">
                    <button 
                        onClick={handleAdd}
                        disabled={!selectedAgentId || isUpdating}
                        className="bg-primary-button text-primary-foreground flex w-full items-center justify-center gap-2 rounded-xl px-6 py-2 text-xs font-black shadow-lg transition-all hover:scale-105 disabled:opacity-50 sm:w-auto"
                    >
                        <UserPlus className="h-4 w-4" /> {t("dashboard.heartbeat_whitelist.grant_button")}
                    </button>
                </div>
            </div>

            {/* List Section */}
            <div className="space-y-3">
                <p className="px-2 text-xs font-black text-muted-foreground uppercase">{t("dashboard.heartbeat_whitelist.current_list_heading")}</p>
                <div className="flex flex-wrap gap-2">
                    {whitelist.map(id => (
                        <div 
                            key={id} 
                            className={cn(
                                "group flex items-center gap-2 px-3 py-1.5 rounded-full border transition-all animate-in zoom-in-95 duration-200",
                                isSoul(id) 
                                    ? "bg-status-warning/10 border-status-warning/30 text-status-warning" 
                                    : "bg-background border-border text-muted-foreground"
                            )}
                        >
                            {isSoul(id) ? <Crown className="h-3 w-3" /> : <ShieldCheck className="h-3 w-3 opacity-50" />}
                            <span className="text-xs font-bold">{id}</span>
                            
                            {isSoul(id) ? (
                                <Lock className="ml-1 h-3 w-3 opacity-40" />
                            ) : (
                                <button 
                                    onClick={() => handleRemove(id)}
                                    className="ml-1 rounded-full p-0.5 opacity-0 transition-colors group-hover:opacity-100 hover:bg-status-error hover:text-white"
                                    title={t("dashboard.heartbeat_whitelist.revoke_title")}
                                >
                                    <Trash2 className="h-3 w-3" />
                                </button>
                            )}
                        </div>
                    ))}
                    {whitelist.length === 0 && !isLoading && (
                        <p className="px-2 text-xs text-muted-foreground italic">{t("dashboard.heartbeat_whitelist.empty_list")}</p>
                    )}
                </div>
            </div>

            <div className="flex items-start gap-2 rounded-xl border border-primary/10 bg-primary/5 p-3">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <p className="text-xs leading-relaxed font-bold text-primary">
                    {t("dashboard.heartbeat_whitelist.security_notice")}
                </p>
            </div>
        </div>
    );
}
