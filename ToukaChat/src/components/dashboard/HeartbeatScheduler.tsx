// src/components/dashboard/HeartbeatScheduler.tsx
"use client";

import React, { useState, useEffect, useCallback } from "react";
import { 
    Clock, Plus, Trash2, Power, 
    Shield, MessageSquare, RefreshCw, AlertTriangle,
    Calendar, Timer, Repeat, X, Home, Edit2, Brain
} from "lucide-react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { formatToTaipeiTime } from "@/lib/time_utils";
import { heartbeatMode } from "@/lib/heartbeat_mode";
import { cn } from "@/lib/utils";
import { useAgents } from "@/lib/providers/AgentProvider";
import { useSessionList } from "@/lib/providers/SessionListProvider";
import { logSSOTError } from "@/lib/error_handler";
import { useHeartbeatSchedules, Schedule } from "@/hooks/useHeartbeatSchedules";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { useTranslation } from "react-i18next";
import { useAuth } from "@/contexts/AuthContext";

// Schedule type imported from useHeartbeatSchedules (SSOT)

type SchedulerMode = "interval" | "daily" | "once";

export function HeartbeatScheduler() {
    const { t } = useTranslation();
    const { agents: allAgents } = useAgents();
    const { user } = useAuth();
    const agents = allAgents.filter(agent => user?.role === "admin" || user?.owned_agents?.includes(agent.id));
    const { sessions } = useSessionList();
    const { schedules, isLoading, isSaving, loadError, mutationError, loadSchedules, saveSchedule, deleteSchedule } = useHeartbeatSchedules();
    const { confirmToast } = useConfirmToast();
    const [isAdding, setIsAdding] = useState(false);
    
    // Edit state
    const [editingId, setEditingId] = useState<string | null>(null);
    const isEditing = !!editingId;
    const [mode, setMode] = useState<SchedulerMode>("interval");
    const [agentId, setAgentId] = useState("");
    const [interval, setIntervalVal] = useState(60);
    const [dailyTimes, setDailyTimes] = useState<string[]>([]);
    const [newDailyTime, setNewDailyTime] = useState("08:00");
    const [onceDatetime, setOnceDatetime] = useState("");
    const [pulseCount, setPulseCount] = useState(1);
    const [pulseSlots, setPulseSlots] = useState<number | null>(null);
    const [securityLevel, setSecurityLevel] = useState("safe");
    const [sessionMode, setSessionMode] = useState<"home" | "independent">("home");
    const [selectedSessionId, setSelectedSessionId] = useState<string>("");
    const [initPrompt, setInitPrompt] = useState(t("dashboard.heartbeat_scheduler.default_init_prompt"));
    const [modelOverride, setModelOverride] = useState<string | null>(null);
    const [availableModels, setAvailableModels] = useState<{ id: string; name?: string }[]>([]);

    const effectiveAgentId = agents.some(agent => agent.id === agentId) ? agentId : (agents[0]?.id ?? "");

    const fetchModels = useCallback(async () => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/models");
            if (res.ok) {
                const data = await res.json();
                setAvailableModels(data.data || []);
            }
        } catch (e) {
            logSSOTError(e, "HeartbeatScheduler:fetchModels");
        }
    }, []);

    const handleAdd = async () => {
        const payload = {
            id: isEditing ? editingId : undefined,
            agent_id: effectiveAgentId,
            mode,
            pulse_count: pulseCount,
            pulse_slots: pulseSlots,
            security_level: securityLevel,
            init_prompt: initPrompt,
            active: true,
            session_mode: sessionMode,
            session_id: sessionMode === "independent" ? selectedSessionId : "home",
            model_override: modelOverride,
            ...(mode === "interval" && { 
                interval_minutes: interval,
            }),
            ...(mode === "daily" && { daily_times: [...dailyTimes] }),
            ...(mode === "once" && { 
                once_datetime: onceDatetime ? new Date(onceDatetime).getTime() : null
            })
        };

        // UX 優化：如果 daily 模式下輸入框還有值且未加入列表，自動幫忙加入
        if (mode === "daily" && newDailyTime && !dailyTimes.includes(newDailyTime)) {
            if (payload.daily_times) {
                payload.daily_times.push(newDailyTime);
                payload.daily_times.sort();
            } else {
                payload.daily_times = [newDailyTime];
            }
        }

        const success = await saveSchedule(isEditing ? editingId : null, payload);
        if (success) {
            setIsAdding(false);
            setEditingId(null);
        }
    };

    const handleToggle = async (id: string) => {
        const schedule = schedules.find(s => s.id === id);
        if (!schedule) return;
        const payload = { ...schedule, active: !schedule.active };
        await saveSchedule(id, payload);
    };

    const handleDeleteClick = (id: string) => {
        confirmToast(t("dashboard.heartbeat_scheduler.confirm_delete_message"), () => deleteSchedule(id), t("dashboard.heartbeat_scheduler.confirm_delete_title"));
    };

    const addDailyTime = () => {
        if (newDailyTime && !dailyTimes.includes(newDailyTime)) {
            setDailyTimes([...dailyTimes, newDailyTime].sort());
        }
    };

    const removeDailyTime = (t: string) => {
        setDailyTimes(dailyTimes.filter(item => item !== t));
    };

    const handleEditInitiate = (s: Schedule) => {
        setEditingId(s.id);
        setMode(s.mode);
        setAgentId(s.agent_id);
        if (s.mode === "interval") {
            setIntervalVal(s.interval_minutes || 60);
        } else if (s.mode === "daily") {
            setDailyTimes(s.daily_times || []);
        } else if (s.mode === "once") {
            if (typeof s.once_datetime === "number") {
                setOnceDatetime(new Date(s.once_datetime).toISOString().slice(0, 16));
            } else {
                setOnceDatetime("");
            }
        }
        setPulseCount(s.pulse_count || 1);
        setPulseSlots(s.pulse_slots ?? null);
        setSecurityLevel(s.security_level ?? "safe");
        setSessionMode(s.session_mode === "independent" ? "independent" : "home");
        setSelectedSessionId(s.session_id || "");
        setInitPrompt(s.init_prompt ?? "");
        setModelOverride(s.model_override || null);
        
        setIsAdding(true);
        window.scrollTo({ top: 0, behavior: 'smooth' });
    };

    const handleCancelEdit = () => {
        setEditingId(null);
        setIsAdding(false);
        setPulseCount(1);
        setPulseSlots(null);
        setInitPrompt(t("dashboard.heartbeat_scheduler.default_init_prompt"));
        setModelOverride(null);
    };

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- 掛載時載入模型清單（非同步 setState 屬預期）
        fetchModels();
    }, [fetchModels]);

    return (
        <div className="space-y-6 rounded-3xl border border-border bg-sidebar p-8 shadow-xl">
            <div className="flex items-center justify-between">
                <h3 className="text-lg font-black text-foreground uppercase tracking-tight italic flex items-center gap-2">
                    <Clock className="w-5 h-5 text-primary" /> {t("dashboard.heartbeat_scheduler.title")}
                </h3>
                <button 
                    onClick={() => isEditing ? handleCancelEdit() : setIsAdding(!isAdding)}
                    className={cn(
                        "flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-black shadow-lg hover:scale-105 transition-transform",
                        isEditing ? "bg-status-warning text-white" : "bg-primary-button text-primary-foreground"
                    )}
                >
                    {isEditing ? <X className="w-4 h-4" /> : <Plus className="w-4 h-4" />} 
                    {isEditing ? t("dashboard.heartbeat_scheduler.cancel_edit") : (isAdding ? t("dashboard.heartbeat_scheduler.cancel") : t("dashboard.heartbeat_scheduler.add_task"))}
                </button>
            </div>

            {(loadError || mutationError) && (
                <div role="alert" className="rounded-xl border border-status-error p-4 text-status-error">
                    <p>{mutationError || loadError}</p>
                    <button onClick={() => void loadSchedules()} disabled={isLoading || isSaving}>
                        {t("dashboard.heartbeat_scheduler.reload", { defaultValue: "重新載入" })}
                    </button>
                </div>
            )}

            {isAdding && (
                <div className={cn(
                    "p-6 bg-background rounded-3xl border-2 space-y-6 animate-in slide-in-from-top duration-300",
                    isEditing ? "border-status-warning/20 shadow-status-warning/5 shadow-2xl" : "border-primary/20"
                )}>
                    <div className="flex items-center gap-2 mb-2 px-1">
                        <div className={cn("w-2 h-2 rounded-full animate-pulse", isEditing ? "bg-status-warning" : "bg-primary")}></div>
                        <span className={cn("text-xs font-black uppercase tracking-widest", isEditing ? "text-status-warning" : "text-primary")}>
                            {isEditing ? t("dashboard.heartbeat_scheduler.editing_schedule") : t("dashboard.heartbeat_scheduler.creating_schedule")}
                        </span>
                    </div>
                    {/* Mode Selection */}
                    <div className="flex bg-sidebar p-1 rounded-2xl border border-border">
                        {([
                            { id: "interval", label: t("dashboard.heartbeat_scheduler.mode_interval"), icon: <Timer className="w-3 h-3" /> },
                            { id: "daily", label: t("dashboard.heartbeat_scheduler.mode_daily"), icon: <Repeat className="w-3 h-3" /> },
                            { id: "once", label: t("dashboard.heartbeat_scheduler.mode_once"), icon: <Calendar className="w-3 h-3" /> }
                        ] as { id: SchedulerMode, label: string, icon: React.ReactNode }[]).map(m => (
                            <button
                                key={m.id}
                                onClick={() => setMode(m.id)}
                                className={cn(
                                    "flex-1 flex items-center justify-center gap-2 py-2 rounded-xl text-xs font-black uppercase transition-all",
                                    mode === m.id ? "bg-primary-button text-primary-foreground shadow-md" : "text-muted-foreground hover:text-primary"
                                )}
                            >
                                {m.icon} {m.label}
                            </button>
                        ))}
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-2">
                            <label className="px-1 text-xs font-black uppercase text-muted-foreground">{t("dashboard.heartbeat_scheduler.label_target_agent")}</label>
                            <select 
                                value={effectiveAgentId}
                                onChange={e => setAgentId(e.target.value)}
                                className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none"
                            >
                                {agents.map(a => (
                                    <option key={a.id} value={a.id}>{a.name}</option>
                                ))}
                            </select>
                        </div>

                        {mode === "interval" && (
                            <div className="space-y-2">
                                <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_interval_minutes")}</label>
                                <input 
                                    type="number"
                                    value={interval}
                                    onChange={e => setIntervalVal(parseInt(e.target.value))}
                                    className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none"
                                />
                            </div>
                        )}

                        {mode === "daily" && (
                            <div className="col-span-2 space-y-3 p-4 bg-sidebar/50 rounded-2xl border border-dashed border-border/50">
                                <div className="space-y-2">
                                    <label htmlFor="daily-time-input" className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_add_time")}</label>
                                    <div className="flex gap-2">
                                        <input 
                                            id="daily-time-input"
                                            type="time"
                                            value={newDailyTime}
                                            onChange={e => setNewDailyTime(e.target.value)}
                                            className="flex-1 bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none"
                                        />
                                        <button 
                                            onClick={addDailyTime} 
                                            aria-label="plus"
                                            className="p-2 bg-primary-button text-white rounded-xl hover:bg-primary/80 transition-colors shadow-lg shadow-primary/20"
                                        >
                                            <Plus className="w-4 h-4" />
                                        </button>
                                    </div>
                                </div>

                                {dailyTimes.length > 0 && (
                                    <div className="space-y-2">
                                        <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_configured_times")}</label>
                                        <div className="flex flex-wrap gap-2">
                                            {dailyTimes.map(t => (
                                                <div key={t} className="flex items-center gap-2 px-3 py-1.5 bg-primary/10 border border-primary/20 rounded-lg text-xs font-bold text-primary animate-in zoom-in duration-200">
                                                    <Clock className="w-3 h-3" />
                                                    {t}
                                                    <button 
                                                        onClick={() => removeDailyTime(t)}
                                                        aria-label={`remove ${t}`}
                                                        className="p-0.5 hover:bg-primary/20 rounded transition-colors"
                                                    >
                                                        <X className="w-3 h-3" />
                                                    </button>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}

                        {mode === "once" && (
                            <div className="space-y-2">
                                <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_execution_time")}</label>
                                <input 
                                    type="datetime-local"
                                    value={onceDatetime}
                                    onChange={e => setOnceDatetime(e.target.value)}
                                    className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none"
                                />
                            </div>
                        )}
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-2">
                            <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_session_mode")}</label>
                            <select 
                                value={sessionMode}
                                onChange={e => setSessionMode(e.target.value as "home" | "independent")}
                                className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none"
                            >
                                <option value="home">{t("dashboard.heartbeat_scheduler.session_mode_home")}</option>
                                <option value="independent">{t("dashboard.heartbeat_scheduler.session_mode_independent")}</option>
                            </select>
                        </div>
                        <div className="space-y-2">
                            <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_model_override")}</label>
                            <select 
                                value={modelOverride || ""}
                                onChange={e => setModelOverride(e.target.value || null)}
                                className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none font-bold"
                            >
                                <option value="">{t("dashboard.heartbeat_scheduler.model_override_default")}</option>
                                {availableModels.map(m => (
                                    <option key={m.id} value={m.id}>{m.name || m.id}</option>
                                ))}
                            </select>
                        </div>
                    </div>

                    <div className="grid grid-cols-3 gap-4">
                        <div className="space-y-2">
                            <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_security_level")}</label>
                            <select 
                                value={securityLevel}
                                onChange={e => setSecurityLevel(e.target.value)}
                                className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none"
                            >
                                <option value="safe">{t("dashboard.heartbeat_scheduler.security_safe")}</option>
                                <option value="full">{t("dashboard.heartbeat_scheduler.security_full")}</option>
                            </select>
                        </div>
                        <div className="space-y-2">
                            <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_pulse_count")}</label>
                            <input 
                                type="number"
                                min="1"
                                value={pulseCount}
                                onChange={e => setPulseCount(parseInt(e.target.value) || 1)}
                                className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none"
                            />
                        </div>
                        <div className="space-y-2">
                            <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_react_slots")}</label>
                            <input 
                                type="number"
                                min="1"
                                placeholder={t("dashboard.heartbeat_scheduler.placeholder_follow_global_budget")}
                                value={pulseSlots || ""}
                                onChange={e => {
                                    const val = e.target.value;
                                    setPulseSlots(val === "" ? null : parseInt(val));
                                }}
                                className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none"
                            />
                            {pulseSlots === null && (
                                <p className="text-xs text-primary/60 mt-1 px-1 font-bold italic">
                                    {t("dashboard.heartbeat_scheduler.budget_hint")}
                                </p>
                            )}
                        </div>
                    </div>

                    {sessionMode === "independent" && (
                        <div className="space-y-2">
                            <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_select_session")}</label>
                            <select 
                                value={selectedSessionId}
                                onChange={e => setSelectedSessionId(e.target.value)}
                                className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none"
                            >
                                <option value="">{t("dashboard.heartbeat_scheduler.select_session_placeholder")}</option>
                                <option value="pulse_channel_default">{t("dashboard.heartbeat_scheduler.default_pulse_channel")}</option>
                                {sessions.map(s => (
                                    <option key={s.id} value={s.id}>{s.title} ({s.id.slice(0, 8)}...)</option>
                                ))}
                            </select>
                        </div>
                    )}

                    <div className="space-y-2">
                        <label className="text-xs font-black text-muted-foreground uppercase px-1">{t("dashboard.heartbeat_scheduler.label_init_prompt")}</label>
                        <textarea 
                            value={initPrompt}
                            onChange={e => setInitPrompt(e.target.value)}
                            className="w-full bg-sidebar border border-input rounded-xl px-4 py-2 text-sm focus:ring-2 ring-primary/50 outline-none h-20 resize-none"
                        />
                    </div>
                    
                    <button 
                        onClick={handleAdd}
                        disabled={isSaving || isLoading || !!loadError || !effectiveAgentId || (mode === "daily" && dailyTimes.length === 0)}
                        className={cn(
                            "w-full py-3 rounded-2xl font-black text-sm shadow-xl disabled:opacity-50 disabled:grayscale transition-all",
                            isEditing ? "bg-status-warning text-white shadow-status-warning/20" : "bg-primary-button text-primary-foreground shadow-primary/20"
                        )}
                    >
                        {isEditing ? t("dashboard.heartbeat_scheduler.submit_edit") : t("dashboard.heartbeat_scheduler.submit_create")}
                    </button>
                </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {schedules.map(s => (
                    <div key={s.id} className={cn(
                        "p-5 rounded-3xl border transition-all relative overflow-hidden flex flex-col justify-between h-full",
                        s.active ? "bg-background border-primary/20 shadow-md" : "bg-background/50 border-border opacity-60"
                    )}>
                        {s.active && <div className="absolute top-0 right-0 w-20 h-20 bg-primary/5 blur-3xl rounded-full -mr-10 -mt-10 animate-pulse"></div>}
                        
                        <div>
                            <div className="flex justify-between items-start mb-4">
                                <div className="flex items-center gap-3">
                                    <div className={cn(
                                        "p-2 rounded-xl",
                                        s.active ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
                                    )}>
                                        {s.mode === "daily" ? <Repeat className="w-5 h-5" /> : s.mode === "once" ? <Calendar className="w-5 h-5" /> : <Timer className="w-5 h-5" />}
                                    </div>
                                    <div>
                                        <h4 className="font-black text-sm text-foreground uppercase tracking-tight italic">Agent: {s.agent_id}</h4>
                                        <div className="text-xs font-bold text-muted-foreground uppercase flex items-center gap-1">
                                            {s.mode === "interval" ? (
                                                <>{t("dashboard.heartbeat_scheduler.interval_every", { minutes: s.interval_minutes })}</>
                                            ) : s.mode === "daily" ? (
                                                <>{t("dashboard.heartbeat_scheduler.daily_times_label")} {s.daily_times?.join(", ")}</>
                                            ) : (
                                                <>{t("dashboard.heartbeat_scheduler.once_trigger_label")} {s.once_datetime ?
                                                    (() => {
                                                        const d = new Date(s.once_datetime);
                                                        return isNaN(d.getTime()) ? "Format Error" : formatToTaipeiTime(d);
                                                    })()
                                                    : "Unknown"}
                                                </>
                                            )}
                                        </div>
                                    </div>
                                </div>
                                <div className="flex gap-2">
                                    <button disabled={isSaving} onClick={() => handleEditInitiate(s)} className="p-2 hover:bg-sidebar rounded-lg transition-colors text-muted-foreground hover:text-status-warning">
                                        <Edit2 className="w-4 h-4" />
                                    </button>
                                    <button disabled={isSaving || !!loadError} onClick={() => handleToggle(s.id)} className="p-2 hover:bg-sidebar rounded-lg transition-colors text-muted-foreground hover:text-primary">
                                        <Power className={cn("w-4 h-4", s.active && "text-primary")} />
                                    </button>
                                    <button disabled={isSaving || !!loadError} onClick={() => handleDeleteClick(s.id)} className="p-2 hover:bg-sidebar rounded-lg transition-colors text-muted-foreground hover:text-status-error">
                                        <Trash2 className="w-4 h-4" />
                                    </button>
                                </div>
                            </div>

                            <div className="space-y-4">
                                <div className="flex flex-wrap items-center gap-3 text-xs">
                                    <div className="flex items-center gap-1 font-black uppercase tracking-widest text-muted-foreground">
                                        <Home className={cn("w-3 h-3", s.session_mode === "home" ? "text-status-warning" : "text-muted-foreground")} />
                                        <span>{s.session_mode === "home" ? "Home" : "Pulse"}</span>
                                    </div>
                                    <div className="flex items-center gap-1 font-black uppercase tracking-widest text-muted-foreground">
                                        <Shield className="w-3 h-3" /> <span className={heartbeatMode(s.security_level) === "YOLO" ? "text-status-warning" : "text-status-success"}>{heartbeatMode(s.security_level)}</span>
                                    </div>
                                    <div className="flex items-center gap-1 font-black uppercase tracking-widest text-muted-foreground">
                                        <Brain className="w-3 h-3" /> <span className="text-primary">{s.model_override ? s.model_override.slice(0, 15) + "..." : "SSOT"}</span>
                                    </div>
                                    <div className="flex items-center gap-1 font-black uppercase tracking-widest text-muted-foreground">
                                        <RefreshCw className="w-3 h-3" /> Pulses: {s.pulse_count || 1} | Slots: {s.pulse_slots}
                                    </div>
                                </div>

                                <div className="p-3 bg-sidebar rounded-2xl border border-border/50 text-xs text-muted-foreground line-clamp-2 italic">
                                    <MessageSquare className="w-3 h-3 inline mr-1 opacity-50" />
                                    {s.init_prompt}
                                </div>
                            </div>
                        </div>

                        <div className="flex justify-between items-center text-xs font-black text-muted-foreground uppercase tracking-widest mt-4 pt-2 border-t border-border/30">
                            <span>{t("dashboard.heartbeat_scheduler.last_run_label")} {(typeof s.last_run === "string" || typeof s.last_run === "number") ?
                                (() => {
                                    const d = new Date(s.last_run);
                                    return isNaN(d.getTime()) ? "Never" : formatToTaipeiTime(d);
                                })()
                                : "Never"}
                            </span>
                            {s.active && <span className="text-primary animate-pulse">Monitoring...</span>}
                        </div>
                    </div>
                ))}

                {schedules.length === 0 && !isLoading && !loadError && (
                    <div className="col-span-full py-20 bg-background/50 rounded-3xl border border-dashed border-border flex flex-col items-center justify-center gap-3 text-muted-foreground">
                        <AlertTriangle className="w-10 h-10 opacity-20" />
                        <p className="text-sm font-bold uppercase tracking-widest opacity-40 italic">{t("dashboard.heartbeat_scheduler.empty_state")}</p>
                    </div>
                )}
            </div>
            
            <div className="flex items-center gap-3 p-4 bg-status-warning/10 border border-status-warning/20 rounded-2xl mt-4">
                <AlertTriangle className="w-5 h-5 text-status-warning shrink-0" />
                <p className="text-xs font-bold uppercase leading-relaxed text-status-warning">
                    {t("dashboard.heartbeat_scheduler.footer_notice")}
                </p>
            </div>
        </div>
    );
}
