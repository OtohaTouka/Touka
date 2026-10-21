// src/components/dashboard/VaultDashboard.tsx
"use client";

import React, { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { 
    Database, Search, HardDrive,
    Layers, RefreshCw,
    ChevronDown, User, ShieldCheck, Settings2
} from "lucide-react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { getErrorMessage } from "@/lib/error_handler";
import { useSystemStatus } from "@/lib/SystemStatusContext";
import { formatToTaipeiTime } from "@/lib/time_utils";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { LegacyMonitorTab } from "@/components/settings/tabs/LegacyMonitorTab";

interface MemoryNode {
    id: number;
    content: string;
    agent_id: string;
    timestamp: number;
    tags: string | string[];
    originalIndex?: number;
}

interface VaultHealth {
    facts?: boolean;
    vector?: boolean;
    legacy?: boolean;
    personal?: boolean;
}

export function VaultDashboard() {
    const { t } = useTranslation();
    const [memories, setMemories] = useState<MemoryNode[]>([]);
    const [health, setHealth] = useState<VaultHealth | null>(null);
    const [isLoading, setIsLoading] = useState(false);
    const { checkStatus } = useSystemStatus();
    // 列表讀取失敗：讀不到不能顯示成空的記憶寶庫；權限不足照錯誤原文，其他失敗請側欄重抓記憶提示
    const [listError, setListError] = useState<{ message: string; forbidden: boolean } | null>(null);
    const [search, setSearch] = useState("");
    const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
    const [expandedAgents, setExpandedAgents] = useState<Set<string>>(new Set());
    const [activeTab, setActiveTab] = useState("explore");

    const failList = React.useCallback(async (error: unknown, status?: number) => {
        const forbidden = status === 403;
        setListError({ message: await getErrorMessage(error), forbidden });
        if (!forbidden) void checkStatus();
    }, [checkStatus]);

    const fetchData = React.useCallback(async () => {
        setIsLoading(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const [memRes, healthRes] = await Promise.all([
                fetchTouka(proxyUrl, "/v1/memory/list"),
                fetchTouka(proxyUrl, "/v1/memory/health")
            ]);
            
            if (memRes.ok) {
                const data = await memRes.json();
                setMemories(data.memories || []);
                setListError(null);
            } else {
                await failList(memRes, memRes.status);
            }
            if (healthRes.ok) {
                setHealth(await healthRes.json());
            }
        } catch (error) {
            await failList(error);
        }
        setIsLoading(false);
    }, [failList]);

    useEffect(() => {
        const timer = setTimeout(() => {
            fetchData();
        }, 0);
        return () => clearTimeout(timer);
    }, [fetchData]);

    const toggleExpand = (index: number) => {
        const newSet = new Set(expandedIds);
        if (newSet.has(index)) newSet.delete(index);
        else newSet.add(index);
        setExpandedIds(newSet);
    };

    const toggleAgent = (agentId: string) => {
        const newSet = new Set(expandedAgents);
        if (newSet.has(agentId)) newSet.delete(agentId);
        else newSet.add(agentId);
        setExpandedAgents(newSet);
    };

    // Safe Grouping & Filtering logic
    const filteredMemories = memories.map((m) => ({ ...m }))
        .filter(m => {
            const content = m.content || "";
            return content.toLowerCase().includes((search || "").toLowerCase());
        });
    
    const grouped: Record<string, MemoryNode[]> = {};
    filteredMemories.forEach(m => {
        const agentId = m.agent_id || "Unknown";
        if (!grouped[agentId]) grouped[agentId] = [];
        grouped[agentId].push(m);
    });

    const isSearching = search.trim().length > 0;

    const getHealthStatus = () => {
        if (!health) return t("dashboard.vault_dashboard.health.probing");
        const parts = [];
        if (health.facts) parts.push(t("dashboard.vault_dashboard.health.facts_ok"));
        if (health.vector) parts.push(t("dashboard.vault_dashboard.health.vector_ok"));
        if (health.legacy) parts.push(t("dashboard.vault_dashboard.health.legacy_ok"));
        if (health.personal) parts.push(t("dashboard.vault_dashboard.health.personal_ok"));
        return parts.length > 0 ? parts.join(" | ") : t("dashboard.vault_dashboard.health.offline");
    };

    const safeParseTags = (tagsStr: string | string[]) => {
        if (!tagsStr) return [];
        if (Array.isArray(tagsStr)) return tagsStr;
        try {
            const parsed = JSON.parse(tagsStr);
            return Array.isArray(parsed) ? parsed : [];
        } catch {
            return [];
        }
    };

    return (
        <div className="flex flex-col h-full bg-background animate-in fade-in duration-500 overflow-hidden font-sans">
            {/* Header */}
            <header className="px-8 py-6 border-b border-border bg-sidebar shrink-0">
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                    <div className="flex items-center gap-4">
                        <div className="p-3 bg-primary/10 rounded-2xl">
                            <Database className="w-6 h-6 text-primary" />
                        </div>
                        <div>
                            <h2 className="italic tracking-tight text-2xl font-black text-foreground uppercase">{t("dashboard.vault_dashboard.title")} <span className="not-italic rounded-full border border-primary/20 bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">Memory Vault</span></h2>
                            <p className="font-bold tracking-widest text-sm text-muted-foreground uppercase opacity-60">{t("dashboard.vault_dashboard.subtitle")}</p>
                        </div>
                    </div>
                    
                    <div className="flex items-center gap-3">
                        <div className="relative">
                            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                            <input 
                                value={search}
                                onChange={(e) => setSearch(e.target.value)}
                                placeholder={t("dashboard.vault_dashboard.search_placeholder")}
                                className="pl-10 pr-4 py-2 bg-background border border-input rounded-xl text-sm outline-none focus:border-primary/50 transition-all w-64 text-foreground"
                            />
                        </div>
                        <button onClick={fetchData} className={cn("p-2 rounded-full hover:bg-background transition-all text-muted-foreground hover:text-primary", isLoading && "animate-spin")}><RefreshCw className="w-5 h-5" /></button>
                    </div>
                </div>
            </header>

            <div className="flex-1 overflow-y-auto p-8 custom-scrollbar">
                <div className="max-w-6xl mx-auto space-y-8">
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                        <VaultInfoCard 
                            icon={<HardDrive className="w-4 h-4" />} 
                            label={t("dashboard.vault_dashboard.cards.storage_engine_status")}
                            value={getHealthStatus()}
                            action={
                                <button
                                    onClick={() => setActiveTab("manage")}
                                    className="p-1 hover:bg-primary/10 rounded-md transition-colors text-primary"
                                    title={t("dashboard.vault_dashboard.go_to_archive_management")}
                                >
                                    <Settings2 className="w-4 h-4" />
                                </button>
                            }
                        />
                        <VaultInfoCard icon={<Layers className="w-4 h-4" />} label={t("dashboard.vault_dashboard.cards.memory_node_count")} value={listError ? "—" : memories.length.toString()} />
                        <VaultInfoCard icon={<ShieldCheck className="w-4 h-4" />} label={t("dashboard.vault_dashboard.cards.security_annotation")} value="Sovereignty Locked" />
                    </div>

                    <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
                        <div className="flex items-center justify-between mb-6">
                            <TabsList className="bg-muted/50 p-1 rounded-xl">
                                <TabsTrigger value="explore" className="rounded-lg px-6 py-2 font-bold data-[state=active]:bg-primary data-[state=active]:text-white">
                                    {t("dashboard.vault_dashboard.tabs.explore")}
                                </TabsTrigger>
                                <TabsTrigger value="manage" className="rounded-lg px-6 py-2 font-bold data-[state=active]:bg-primary data-[state=active]:text-white">
                                    {t("dashboard.vault_dashboard.tabs.manage")}
                                </TabsTrigger>
                            </TabsList>
                        </div>

                        <TabsContent value="explore" className="mt-0 outline-none space-y-6">
                            {/* Memory Groups */}
                            <div className="space-y-6">
                                {listError ? (
                                    <div role="alert" data-testid="vault-list-error" className="rounded-2xl border border-destructive bg-destructive/10 p-6 text-sm text-destructive">
                                        {listError.forbidden ? listError.message : t("dashboard.vault_dashboard.list_failed", { error: listError.message })}
                                    </div>
                                ) : Object.keys(grouped).length > 0 ? (
                                    Object.entries(grouped).map(([agentId, items]) => {
                                        const isAgentExpanded = isSearching || expandedAgents.has(agentId);
                                        return (
                                            <section key={agentId} className="space-y-4">
                                                <button 
                                                    onClick={() => toggleAgent(agentId)}
                                                    className="w-full flex items-center gap-3 px-4 py-3 bg-primary/5 border-l-4 border-primary rounded-r-lg hover:bg-primary/10 transition-colors"
                                                >
                                                    <div className={cn("transition-transform duration-300", isAgentExpanded && "rotate-180")}>
                                                        <ChevronDown className="w-4 h-4 text-primary" />
                                                    </div>
                                                    <User className="w-4 h-4 text-primary" />
                                                    <h3 className="tracking-widest text-sm font-black uppercase text-foreground/80">Agent: {agentId}</h3>
                                                    <span className="rounded-full border border-border bg-background px-2 py-0.5 text-xs font-bold text-muted-foreground">{t("dashboard.vault_dashboard.memory_count", { count: items.length })}</span>
                                                </button>

                                                {isAgentExpanded && (
                                                    <div className="grid grid-cols-1 gap-2 pl-4 animate-in slide-in-from-left-2 duration-300">
                                                        {items.map((m, idx) => {
                                                            const isExpanded = isSearching || expandedIds.has(idx);
                                                            return (
                                                                <div 
                                                                    key={idx} 
                                                                    className={cn(
                                                                        "group border border-border rounded-2xl overflow-hidden transition-all duration-300",
                                                                        isExpanded ? "bg-background shadow-md border-primary/20" : "bg-sidebar/50 hover:bg-sidebar hover:border-primary/40 shadow-sm"
                                                                    )}
                                                                >
                                                                    {/* Item Header */}
                                                                    <button 
                                                                        onClick={() => toggleExpand(idx)}
                                                                        className="w-full flex items-center justify-between px-6 py-4 text-left"
                                                                    >
                                                                        <div className="flex items-center gap-4">
                                                                            <div className={cn("transition-transform duration-300", isExpanded && "rotate-180")}>
                                                                                <ChevronDown className="w-4 h-4 text-muted-foreground" />
                                                                            </div>
                                                                            <span className="font-mono tracking-tight text-xs font-black text-muted-foreground uppercase">
                                                                                {formatToTaipeiTime(m.timestamp)}
                                                                            </span>
                                                                            {!isExpanded && !isSearching && (
                                                                                <span className="text-sm text-muted-foreground italic line-clamp-1 opacity-60">
                                                                                    — {(m.content || "").substring(0, 50)}...
                                                                                </span>
                                                                            )}
                                                                        </div>
                                                                        <div className="flex items-center gap-2">
                                                                            <span className="rounded border border-border bg-background px-2 py-0.5 tracking-widest text-xs font-black text-muted-foreground uppercase">Persistent</span>
                                                                        </div>
                                                                    </button>

                                                                    {/* Item Content */}
                                                                    {isExpanded && (
                                                                        <div className="px-14 pb-6 animate-in slide-in-from-top-2 duration-300">
                                                                            <div className="p-5 bg-muted/30 rounded-xl border border-border/50 text-sm text-foreground leading-relaxed whitespace-pre-wrap selection:bg-primary/20">
                                                                                {m.content || t("dashboard.vault_dashboard.no_content")}
                                                                            </div>
                                                                            {safeParseTags(m.tags).length > 0 && (
                                                                                <div className="flex flex-wrap gap-2 mt-4">
                                                                                    {safeParseTags(m.tags).map((tag: string, ti: number) => (
                                                                                        <span key={ti} className="rounded-md bg-primary/10 px-2 py-0.5 font-bold text-xs text-primary">#{tag}</span>
                                                                                    ))}
                                                                                </div>
                                                                            )}
                                                                        </div>
                                                                    )}
                                                                </div>
                                                            );
                                                        })}
                                                    </div>
                                                )}
                                            </section>
                                        );
                                    })
                                ) : (
                                    <div className="flex flex-col items-center justify-center py-32 text-muted-foreground space-y-4">
                                        <Database className="w-12 h-12 opacity-20" />
                                        <p className="italic text-sm">{t("dashboard.vault_dashboard.empty_state")}</p>
                                    </div>
                                )}
                            </div>
                        </TabsContent>

                        <TabsContent value="manage" className="mt-0 outline-none">
                            <div className="bg-sidebar rounded-3xl border border-border p-8 shadow-xl">
                                <LegacyMonitorTab />
                            </div>
                        </TabsContent>
                    </Tabs>
                </div>
            </div>
        </div>
    );
}

interface VaultInfoCardProps {
    icon: React.ReactNode;
    label: string;
    value: string;
    action?: React.ReactNode;
}

function VaultInfoCard({ icon, label, value, action }: VaultInfoCardProps) {
    return (
        <div className="p-6 bg-sidebar rounded-3xl border border-border shadow-lg flex items-center justify-between">
            <div className="flex items-center gap-4">
                <div className="p-3 bg-primary/10 rounded-xl text-primary">{icon}</div>
                <div>
                    <span className="tracking-widest text-xs font-black text-muted-foreground uppercase">{label}</span>
                    <div className="text-lg font-black text-foreground leading-tight">{value}</div>
                </div>
            </div>
            {action && <div>{action}</div>}
        </div>
    );
}
