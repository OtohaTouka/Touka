// src/components/layout/SoulStudio.tsx
"use client";

import { useState, useEffect, useCallback, useRef, useLayoutEffect, useId, useSyncExternalStore } from "react";
import {
    UserCircle2, Cpu, PlusCircle, X,
    Info, Check, RefreshCw, Zap, Brain,
    Wrench, Box, Pencil, Trash2, Save, Loader2, ShieldAlert
} from "lucide-react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { getSystemEpoch } from "@/lib/time_utils";
import { useAgents } from "@/lib/providers/AgentProvider";
import { SafetySettingsGrid } from "@/components/studio/SafetySettingsGrid";
import { SystemPromptEditor } from "@/components/studio/SystemPromptEditor";
import { BackupRetentionNotice } from "@/components/common/BackupRetentionNotice";
import { logSSOTError, getErrorMessage, getCollisionMessage, errorMsg } from "@/lib/error_handler";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { useSoulSave } from "@/hooks/useSoulSave";
import { isSelectableBaseModel } from "@/lib/default_model";
import {
    Dialog,
    DialogContent,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";

interface SoulConfig {
    id: string;
    originalId?: string;
    name: string;
    description: string;
    persona: string;
    /** 你指定的模型（config.base_model）；空字串＝沒指定 */
    baseModel: string;
    /** 目前在用的模型（active_brain，模型自己可以換）；null＝沒設 */
    activeBrain: string | null;
    temperature: number;
    topP: number;
    topK: number;
    safetySettings?: Array<{ category: string, threshold: string }>;
    enabledTools: string[];
}

const DEFAULT_SOUL_CONFIG: SoulConfig = {
    id: "",
    name: "新靈魂核心",
    description: "系統核心人格設定",
    persona: "你是一個專業、理性且具備高度共情能力的 AI 助手。",
    baseModel: "",
    activeBrain: null,
    temperature: 0.7,
    topP: 1.0,
    topK: 40,
    enabledTools: [],
    safetySettings: [
        { category: "HARM_CATEGORY_HARASSMENT", threshold: "BLOCK_MEDIUM_AND_ABOVE" },
        { category: "HARM_CATEGORY_HATE_SPEECH", threshold: "BLOCK_MEDIUM_AND_ABOVE" },
        { category: "HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold: "BLOCK_MEDIUM_AND_ABOVE" },
        { category: "HARM_CATEGORY_DANGEROUS_CONTENT", threshold: "BLOCK_MEDIUM_AND_ABOVE" }
    ]
};

function subscribeProxy(notify: () => void) {
    window.addEventListener('touka_connections_updated', notify);
    window.addEventListener('storage', notify);
    return () => {
        window.removeEventListener('touka_connections_updated', notify);
        window.removeEventListener('storage', notify);
    };
}

const SAVE_FIELDS = ['persona', 'baseModel', 'temperature', 'topP', 'topK', 'enabledTools', 'safetySettings'] as const;

export function SoulStudio() {
    const { t } = useTranslation();
    const { agents: allAgents, refreshAgents } = useAgents();
    const [souls, setSouls] = useState<SoulConfig[]>([]);
    const [activeSoulId, setActiveSoulId] = useState<string | null>(null);
    const [availableBaseModels, setAvailableBaseModels] = useState<string[]>([]);
    const [dynamicTools, setDynamicTools] = useState<{ name: string, desc: string, category: string }[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    
    // 建立新靈魂的表單狀態
    const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
    const [creationForm, setCreationForm] = useState<SoulConfig>({ ...DEFAULT_SOUL_CONFIG, id: `soul-${getSystemEpoch()}` });

    // C4：詳情頁編輯模式＋刪除；步升密碼確認 modal（編輯儲存與刪除共用）
    const [isEditing, setIsEditing] = useState(false);
    const [editForm, setEditForm] = useState<SoulConfig | null>(null);
    // pendingAction 決定密碼 modal 確認後執行哪個動作（null＝modal 關閉）
    const [pendingAction, setPendingAction] = useState<"delete" | "save" | null>(null);
    const [confirmPassword, setConfirmPassword] = useState("");
    const [actionBusy, setActionBusy] = useState(false);
    const saveProxyUrl = useSyncExternalStore(subscribeProxy, getActiveProxyUrl, getActiveProxyUrl);
    const [soulsProxyUrl, setSoulsProxyUrl] = useState<string | null>(null);
    const activeSoul = soulsProxyUrl === saveProxyUrl
        ? souls.find(s => s.id === (activeSoulId || souls[0]?.id)) || souls[0] : undefined;
    const readGeneration = useRef(0);
    const loadingProxy = useRef<string | null>(null);
    const detailScroll = useRef<HTMLDivElement>(null);
    const editButton = useRef<HTMLButtonElement>(null);
    const editScroll = useRef(0);
    const passwordInput = useRef<HTMLInputElement>(null);
    const feedbackId = useId();

    const loadModelsAndTools = useCallback(async () => {
        try {
            const proxyUrl = getActiveProxyUrl();
            // 1. 基礎模型
            const mRes = await fetchTouka(proxyUrl, "/v1/models");
            if (mRes.ok) {
                const data = await mRes.json();
                if (data.data) {
                    // 只列可當主要模型的一般模型（規則和模型工坊共用，見 isSelectableBaseModel）
                    const ids = data.data
                        .filter(isSelectableBaseModel)
                        .map((m: { id: string }) => m.id);
                    setAvailableBaseModels(ids);
                    
                    // 如果目前表單的大腦不在清單中且清單不為空，設為第一個可啟動的大腦
                    if (ids.length > 0 && !ids.includes(creationForm.baseModel)) {
                        setCreationForm(prev => ({ ...prev, baseModel: ids[0] }));
                    }
                }
            }
            // 2. 工具 (SSOT 動態獲取)
            const tRes = await fetchTouka(proxyUrl, "/v1/tools_stats");
            if (tRes.ok) {
                const data = await tRes.json();
                const flattened = [
                    ...(data.builtin_schemas || []),
                    ...(data.memory_schemas || []),
                    ...(data.plugin_schemas || []),
                    ...(data.mcp_schemas || [])
                ].map((s: { function?: { name?: string; description?: string }; category?: string }) => ({
                    name: s.function?.name || "Unknown",
                    desc: s.function?.description || "",
                    category: s.category || "系統"
                }));
                setDynamicTools(flattened);
            }
        } catch (err) {
            logSSOTError(err, "SoulStudio:loadModelsAndTools");
        }
    }, [creationForm.baseModel]);

    const loadSouls = useCallback(async (options: {
        background?: boolean; signal?: AbortSignal; current?: () => boolean;
    } = {}): Promise<boolean> => {
        const generation = ++readGeneration.current;
        const proxyUrl = getActiveProxyUrl();
        const current = () => generation === readGeneration.current && !options.signal?.aborted
            && (!options.current || options.current()) && getActiveProxyUrl() === proxyUrl;
        if (!options.background) setIsLoading(true);
        try {
            const soulRes = await fetchTouka(proxyUrl, "/v1/souls", { signal: options.signal });
            if (soulRes.ok) {
                const soulData = await soulRes.json();
                if (!current()) return false;
                if (soulData.souls && Array.isArray(soulData.souls)) {
                    // 智慧過濾：如果 allAgents 已載入且有資料，則進行嚴格過濾；否則先顯示原始資料以避免空白
                    const soulsToMap = allAgents.length > 0 
                        ? soulData.souls.filter((s: { id: string }) => allAgents.some(a => (a.id === s.id || `ToukaSoul${a.id}` === s.id) && (a.type === 'soul' || a.type === 'soul_placeholder')))
                        : soulData.souls;

                    const filteredSouls: SoulConfig[] = soulsToMap
                        .map((s: { id: string, description?: string, persona?: string, active_brain?: string | null, config?: { base_model?: string | null, temperature?: number, top_p?: number, top_k?: number, safety_settings?: unknown[], enabled_tools?: string[] } }) => ({
                            id: s.id,
                            name: s.id,
                            description: s.description || "",
                            persona: s.persona || "",
                            baseModel: s.config?.base_model ?? "",
                            activeBrain: s.active_brain ?? null,
                            temperature: s.config?.temperature ?? 0.7,
                            topP: s.config?.top_p ?? 1.0,
                            topK: s.config?.top_k ?? 40,
                            enabledTools: s.config?.enabled_tools || [],
                            safetySettings: s.config?.safety_settings || [...DEFAULT_SOUL_CONFIG.safetySettings!]
                        }));

                    if (options.background && filteredSouls.filter(s => s.id === activeSoul?.id).length !== 1) return false;
                    
                    setSouls(filteredSouls);
                    setSoulsProxyUrl(proxyUrl);
                    if (filteredSouls.length > 0 && !activeSoulId) {
                        setActiveSoulId(filteredSouls[0].id);
                    }
                    return true;
                }
            }
        } catch (err) {
            if (!options.background && current()) {
                logSSOTError(err, "SoulStudio:loadSouls");
                toast.error(errorMsg("SYSTEM_ERROR"));
            }
        } finally {
            if (!options.background && current()) setIsLoading(false);
        }
        return false;
    }, [activeSoulId, activeSoul?.id, allAgents]);

    const readback = useCallback(async (signal: AbortSignal, current: () => boolean) => {
        if (!(await refreshAgents()) || !current()) return false;
        window.dispatchEvent(new Event("touka_refresh_models"));
        return loadSouls({ background: true, signal, current });
    }, [refreshAgents, loadSouls]);
    const soulSave = useSoulSave({ proxyUrl: saveProxyUrl, soulId: activeSoul?.id ?? null, readback });
    const busy = actionBusy || soulSave.busy;
    const feedback = soulSave.feedback;
    const unresolved = feedback.phase === 'unknown' && !feedback.reconciled;

    useEffect(() => {
        if (passwordInput.current) passwordInput.current.value = '';
        setPendingAction(null);
        setConfirmPassword("");
        setIsEditing(false);
        setEditForm(null);
    }, [saveProxyUrl, activeSoul?.id]);

    useEffect(() => {
        setSouls([]);
        setSoulsProxyUrl(null);
        return () => { readGeneration.current++; };
    }, [saveProxyUrl]);

    useLayoutEffect(() => {
        if (feedback.phase === 'saved' && !isEditing) {
            editButton.current?.focus({ preventScroll: true });
            if (detailScroll.current) detailScroll.current.scrollTop = editScroll.current;
        }
    }, [feedback.phase, isEditing]);

    useEffect(() => {
        if (feedback.phase === 'error' && feedback.passwordError) passwordInput.current?.focus({ preventScroll: true });
    }, [feedback]);

    useEffect(() => {
        const proxyChanged = loadingProxy.current !== saveProxyUrl;
        loadingProxy.current = saveProxyUrl;
        if (!proxyChanged && (feedback.phase === 'saving' || feedback.phase === 'saved' || feedback.phase === 'unknown')) return;
        loadSouls();
        loadModelsAndTools();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [allAgents.length, saveProxyUrl]);

    const differentFields = activeSoul && editForm ? SAVE_FIELDS.filter(
        field => JSON.stringify(activeSoul[field]) !== JSON.stringify(editForm[field]),
    ) : [];
    const reconciliation = feedback.phase === 'unknown' && feedback.reconciled && activeSoul && editForm ? (
        <p>{differentFields.length === 0
            ? t('layout.soul_studio.read_matches')
            : t('layout.soul_studio.read_differs', {
                fields: differentFields.map(field => t(`layout.soul_studio.compare_fields.${field}`)).join('、'),
            })} {t('layout.soul_studio.read_not_auth')}</p>
    ) : null;


    const handleCreateSoul = async () => {
        if (!creationForm.id.trim()) return;
        setIsLoading(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, "/v1/souls", {
                method: "POST",
                body: JSON.stringify({
                    agent_id: creationForm.id.trim(),
                    name: creationForm.name,
                    persona: creationForm.persona,
                    config: {
                        base_model: creationForm.baseModel,
                        temperature: creationForm.temperature,
                        top_p: creationForm.topP,
                        top_k: creationForm.topK,
                        enabled_tools: creationForm.enabledTools,
                        safety_settings: creationForm.safetySettings
                    }
                })
            });
            
            if (res.ok) {
                await refreshAgents();
                // BUG3 前端半（soul-crud repair C2）：ModelProvider 監聽此事件重抓 /v1/models，
                // 新 ToukaSoul<id> 立即出現在聊天模型選單——否則建立成功仍「選不到＝不能說話」。
                window.dispatchEvent(new Event("touka_refresh_models"));
                setIsCreateModalOpen(false);
                setCreationForm({ ...DEFAULT_SOUL_CONFIG, id: `soul-${getSystemEpoch()}` });
                await loadSouls();
                setActiveSoulId(creationForm.id.trim());
            } else {
                const errMsg = (await getCollisionMessage(res)) ?? (await getErrorMessage(res, "CONFIG_SAVE_FAILED"));
                toast.error(errMsg);
            }
        } catch (e) {
            logSSOTError(e, "SoulStudio:handleCreateSoul");
            toast.error(await getErrorMessage(e));
        } finally {
            setIsLoading(false);
        }
    };

    const startEdit = (soul: SoulConfig) => {
        soulSave.reset();
        editScroll.current = detailScroll.current?.scrollTop ?? 0;
        setEditForm({ ...soul });
        setIsEditing(true);
    };

    const cancelEdit = () => {
        if (busy) return;
        soulSave.reset();
        setIsEditing(false);
        setEditForm(null);
    };

    // 密碼 modal：取消/關閉
    const closeConfirm = () => {
        if (busy) return;
        if (pendingAction === 'save' && feedback.phase === 'error') soulSave.reset();
        setPendingAction(null);
        setConfirmPassword("");
    };

    // 密碼 modal：確認後依 pendingAction 執行編輯儲存或刪除（皆帶 admin_password 步升）
    const confirmAction = async () => {
        if (!activeSoul || !pendingAction || !confirmPassword || busy) return;
        if (pendingAction === "save") {
            if (unresolved || feedback.refreshing) return;
            readGeneration.current++;
            setIsLoading(false);
            const form = editForm ?? activeSoul;
            const outcome = await soulSave.save({
                persona: form.persona,
                config: {
                    base_model: form.baseModel, temperature: form.temperature,
                    top_p: form.topP, top_k: form.topK,
                    enabled_tools: form.enabledTools, safety_settings: form.safetySettings,
                    is_toukasoul: true,
                },
                admin_password: confirmPassword,
            });
            if (outcome === 'stale' || outcome === 'ignored') return;
            setConfirmPassword("");
            if (outcome === 'saved') {
                setSouls(previous => previous.map(soul => soul.id === form.id ? { ...form } : soul));
                setIsEditing(false);
                setEditForm(null);
                setPendingAction(null);
                void soulSave.refresh();
            }
            return;
        }
        setActionBusy(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            if (pendingAction === "delete") {
                const res = await fetchTouka(proxyUrl, `/v1/souls/${encodeURIComponent(activeSoul.id)}`, {
                    method: "DELETE",
                    body: JSON.stringify({ admin_password: confirmPassword }),
                });
                if (!res.ok) {
                    toast.error(await getErrorMessage(res, "SYSTEM_ERROR"));
                    return;
                }
                toast.success(t("layout.soul_studio.delete_success", { id: activeSoul.id }), {
                    description: t("backup_notice.still_there_delete", { id: activeSoul.id }),
                });
                setActiveSoulId(null);
                setIsEditing(false);
                setEditForm(null);
            }
            // 兩動作皆刷新：souls 列表＋agents＋模型選單（刪除/改名後聊天選單同步）
            setPendingAction(null);
            setConfirmPassword("");
            await refreshAgents();
            window.dispatchEvent(new Event("touka_refresh_models"));
            await loadSouls();
        } catch (e) {
            logSSOTError(e, "SoulStudio:confirmAction");
            toast.error(await getErrorMessage(e));
        } finally {
            setActionBusy(false);
        }
    };

    if (isLoading && souls.length === 0) {
        return (
            <div className="h-full w-full flex items-center justify-center bg-background">
                <RefreshCw className="w-8 h-8 text-primary animate-spin" />
            </div>
        );
    }

    const isEmpty = souls.length === 0;

    return (
        <div className="flex flex-col h-full bg-background overflow-hidden font-sans">
            {/* Header: Unified for both empty and normal states */}
            <header className="h-16 border-b border-border flex items-center justify-between px-6 bg-sidebar sticky top-0 z-10">
                <div className="flex items-center gap-4 flex-1">
                    {isEmpty ? (
                        <div className="flex items-center gap-4 flex-1">
                            <UserCircle2 className="w-5 h-5 text-primary opacity-50" />
                            <span className="text-sm font-bold text-muted-foreground italic">{t("layout.soul_studio.no_core_soul_detected_short")}</span>
                        </div>
                    ) : (
                        <div className="flex items-center gap-2 px-3 py-1.5 bg-muted/50 rounded-lg border border-border shadow-sm transition-all focus-within:ring-2 focus-within:ring-primary/20">
                            <UserCircle2 className="w-5 h-5 text-primary" />
                            <select 
                                value={activeSoulId || ""} 
                                onChange={(e) => setActiveSoulId(e.target.value)}
                                disabled={isEditing || busy}
                                className="bg-transparent border-none text-base font-bold focus:ring-0 cursor-pointer w-56 text-foreground appearance-none"
                            >
                                {souls.map(s => (
                                    <option key={s.id} value={s.id} className="bg-popover text-foreground">
                                        {s.id}
                                    </option>
                                ))}
                            </select>
                        </div>
                    )}
                    <button 
                        onClick={() => setIsCreateModalOpen(true)} 
                        className="p-2 hover:bg-background rounded-full transition-colors text-muted-foreground hover:text-primary" 
                        title={t("layout.soul_studio.add_core")}
                    >
                        <PlusCircle className="w-5 h-5" />
                    </button>
                </div>
                
                {!isEmpty && activeSoul && (
                    <div className="flex items-center gap-2">
                        {isEditing ? (
                            <>
                                <button
                                    onClick={cancelEdit}
                                    disabled={busy}
                                    className="px-4 py-2 rounded-xl text-xs font-bold text-muted-foreground hover:bg-background transition-all uppercase tracking-widest"
                                >
                                    {t("layout.soul_studio.cancel_edit")}
                                </button>
                                <button
                                    onClick={() => { setConfirmPassword(""); setPendingAction("save"); }}
                                    disabled={busy}
                                    className="px-4 py-2 bg-primary-button text-white rounded-xl flex items-center gap-2 text-xs font-black uppercase tracking-widest hover:opacity-90 transition-all"
                                >
                                    <Save className="w-4 h-4" /> {t("layout.soul_studio.save_changes")}
                                </button>
                            </>
                        ) : (
                            <>
                                <button
                                    title={t("layout.soul_studio.edit_soul")}
                                    ref={editButton}
                                    onClick={() => startEdit(activeSoul)}
                                    className="p-2 rounded-xl text-muted-foreground hover:text-primary hover:bg-primary/10 transition-all"
                                >
                                    <Pencil className="w-5 h-5" />
                                </button>
                                <button
                                    title={t("layout.soul_studio.delete_soul")}
                                    onClick={() => { setConfirmPassword(""); setPendingAction("delete"); }}
                                    className="p-2 rounded-xl text-muted-foreground hover:text-status-error hover:bg-status-error/10 transition-all"
                                >
                                    <Trash2 className="w-5 h-5" />
                                </button>
                            </>
                        )}
                    </div>
                )}
            </header>

            <div className="shrink-0 space-y-2 px-6" aria-live="polite">
                {feedback.phase === 'saved' && (
                    <div role="status" className="rounded-xl border border-border bg-sidebar p-4">
                        {t("layout.soul_studio.save_confirmed")}
                    </div>
                )}
                {feedback.phase === 'saved' && feedback.refreshFailed && (
                    <div role="alert" className="rounded-xl border border-border p-4">
                        <p>{t("layout.soul_studio.saved_refresh_failed")}</p>
                        <button onClick={() => void soulSave.refresh()} disabled={feedback.refreshing}>
                            {t("layout.soul_studio.read_again")}
                        </button>
                    </div>
                )}
                {feedback.phase === 'unknown' && pendingAction === null && (
                    <div role="alert" className="rounded-xl border border-border p-4">
                        <p>{t("layout.soul_studio.save_unknown")}</p>
                        {reconciliation}
                        {feedback.refreshFailed && <p>{t("layout.soul_studio.read_failed")}</p>}
                        <button onClick={() => void soulSave.refresh()} disabled={feedback.refreshing}>
                            {t("layout.soul_studio.read_again")}
                        </button>
                    </div>
                )}
            </div>
            <div ref={detailScroll} className="flex-1 overflow-y-auto p-6 space-y-4 custom-scrollbar">
                {!isLoading && isEmpty ? (
                    <div className="flex flex-col items-center justify-center h-full p-8 text-muted-foreground gap-4">
                        <div className="p-12 border-2 border-dashed border-border rounded-3xl text-center max-w-md bg-sidebar/30">
                            <Info className="w-12 h-12 mx-auto mb-4 text-primary/20" />
                            <h3 className="text-base font-black text-foreground uppercase tracking-widest mb-2">Soul Core Synchronizing</h3>
                            <p className="text-xs leading-relaxed opacity-60">
                                {t("layout.soul_studio.empty_state_description")}
                            </p>
                            <button onClick={() => void loadSouls()} className="mt-6 px-6 py-2 bg-primary/10 text-primary text-xs font-bold rounded-xl hover:bg-primary/20 transition-all border border-primary/20">{t("layout.soul_studio.resync")}</button>
                        </div>
                    </div>
                ) : activeSoul ? (
                    <div inert={soulSave.busy} className="max-w-7xl mx-auto space-y-4 py-1 animate-in fade-in duration-300">
                        <div className="flex flex-col md:flex-row md:items-end justify-between gap-6 border-b border-border pb-4">
                            <div className="space-y-2">
                                <div className="flex items-center gap-3">
                                    <h2 className="text-4xl font-black text-foreground uppercase tracking-tighter italic">{activeSoul.id}</h2>
                                    <span className="bg-primary/10 text-primary px-3 py-1 rounded-full text-xs font-black uppercase tracking-widest border border-primary/20 shadow-sm">Soul Core</span>
                                </div>
                                <p className="text-muted-foreground font-bold uppercase tracking-widest text-xs opacity-70">{t("layout.soul_studio.soul_subtitle")}</p>
                            </div>
                        </div>

                        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
                            <div className="lg:col-span-2 space-y-6">
                                <section className="space-y-4">
                                    <h3 className="text-xs font-black text-muted-foreground uppercase tracking-widest flex items-center gap-2"><Cpu className="w-3 h-3" /> {t("layout.soul_studio.section_touka_soul")}</h3>
                                    <div className="p-6 bg-sidebar rounded-3xl border border-border shadow-2xl shadow-black/5 relative overflow-hidden group">
                                        <div className="absolute top-0 left-0 w-1.5 h-full bg-primary opacity-50"></div>
                                        <textarea
                                            className="w-full h-96 bg-background/50 border border-input rounded-2xl p-5 text-sm leading-relaxed outline-none focus:border-primary/50 transition-all font-medium text-foreground custom-scrollbar resize-none shadow-inner"
                                            value={isEditing && editForm ? editForm.persona : activeSoul.persona}
                                            readOnly={!isEditing || soulSave.busy}
                                            onChange={(e) => setEditForm(f => f ? { ...f, persona: e.target.value } : f)}
                                            placeholder={t("layout.soul_studio.persona_loading_placeholder")}
                                        />
                                        <div className="mt-4 flex items-center justify-between text-xs font-bold text-muted-foreground uppercase tracking-widest px-1">
                                            <span>{isEditing ? t("layout.soul_studio.edit_mode_badge") : "Read Only • System Protected"}</span>
                                            <span>{(isEditing && editForm ? editForm.persona : activeSoul.persona).length} Characters</span>
                                        </div>
                                    </div>
                                </section>
                            </div>

                            <div className="space-y-8">
                                <section className="space-y-4">
                                    <h3 className="text-xs font-black text-muted-foreground uppercase tracking-widest flex items-center gap-2"><Zap className="w-3 h-3" /> {t("layout.soul_studio.section_configuration")}</h3>
                                    <div className="p-4 bg-sidebar rounded-3xl border border-border shadow-xl space-y-4">
                                        <div className="space-y-4">
                                            {/* ADR-0250：你指定的（config.base_model）／目前在用的（active_brain）分兩欄 */}
                                            <div role="group" aria-label={t("layout.soul_studio.chosen_model")} className="space-y-2">
                                                <label className="text-xs font-black text-muted-foreground uppercase tracking-widest ml-1">{t("layout.soul_studio.chosen_model")}</label>
                                                {isEditing && editForm ? (
                                                    <select
                                                        aria-label={t("layout.soul_studio.chosen_model")}
                                                        className="w-full bg-background border border-input rounded-xl px-4 py-2.5 text-xs font-bold text-foreground outline-none focus:ring-2 focus:ring-primary/20 appearance-none cursor-pointer"
                                                        value={editForm.baseModel}
                                                        onChange={(e) => setEditForm(f => f ? { ...f, baseModel: e.target.value } : f)}
                                                    >
                                                        {!availableBaseModels.includes(editForm.baseModel) && (
                                                            <option value={editForm.baseModel} disabled>{editForm.baseModel || t("layout.soul_studio.not_configured")}</option>
                                                        )}
                                                        {availableBaseModels.map(id => (
                                                            <option key={id} value={id}>{id}</option>
                                                        ))}
                                                    </select>
                                                ) : (
                                                    <div className="w-full bg-background border border-border rounded-xl px-4 py-2.5 text-xs font-mono font-bold text-foreground">
                                                        {activeSoul.baseModel || t("layout.soul_studio.not_configured")}
                                                    </div>
                                                )}
                                            </div>
                                            <div role="group" aria-label={t("layout.soul_studio.model_in_use")} className="space-y-2">
                                                <label className="text-xs font-black text-muted-foreground uppercase tracking-widest ml-1">{t("layout.soul_studio.model_in_use")}</label>
                                                <div className="w-full bg-background border border-border rounded-xl px-4 py-2.5 text-xs font-mono font-bold text-primary flex items-center gap-2">
                                                    <Brain className="w-3.5 h-3.5" />
                                                    {activeSoul.activeBrain ?? t("layout.soul_studio.not_configured")}
                                                </div>
                                                {activeSoul.baseModel && activeSoul.activeBrain && activeSoul.activeBrain !== activeSoul.baseModel && (
                                                    <p className="text-xs text-muted-foreground ml-1">{t("layout.soul_studio.model_in_use_differs")}</p>
                                                )}
                                            </div>
                                        </div>

                                        <div className="pt-4 border-t border-border">
                                            <div className="flex items-center justify-between text-xs font-black text-muted-foreground uppercase tracking-widest mb-3">
                                                <span>Abilities</span>
                                                <span className="text-primary">{activeSoul.enabledTools.length} Active</span>
                                            </div>
                                            <div className="flex flex-wrap gap-1.5">
                                                {activeSoul.enabledTools.map(t => (
                                                    <span key={t} className="px-2 py-1 bg-background border border-border rounded-lg text-xs font-bold text-foreground/80">{t}</span>
                                                ))}
                                            </div>
                                        </div>
                                    </div>
                                </section>

                                <div className="p-4 bg-primary/5 border border-primary/10 rounded-3xl space-y-3">
                                    <h4 className="text-xs font-black text-primary uppercase tracking-widest flex items-center gap-2"><Info className="w-3 h-3" /> {t("layout.soul_studio.sync_explanation_title")}</h4>
                                    <p className="text-xs text-muted-foreground font-medium leading-relaxed">{t("layout.soul_studio.sync_explanation_body")}</p>
                                </div>
                            </div>
                        </div>
                    </div>
                ) : null}
            </div>

            {isCreateModalOpen && (
                // WCAG 2.1.2（A3）：手寫覆蓋層 → Radix Dialog（焦點陷阱/Esc/焦點歸位內建）
                <Dialog open onOpenChange={(open) => { if (!open) setIsCreateModalOpen(false); }}>
                    <DialogContent
                        overlayClassName="bg-background/80 backdrop-blur-md"
                        className="bg-sidebar w-full max-w-4xl max-h-[85vh] rounded-3xl border border-border shadow-2xl flex flex-col overflow-hidden"
                    >
                        <div className="absolute top-0 right-0 w-64 h-64 bg-primary/5 rounded-full blur-3xl -mr-32 -mt-32"></div>

                        <div className="flex justify-between items-center p-8 border-b border-border relative z-10">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-primary/10 rounded-xl text-primary">
                                    <PlusCircle className="w-6 h-6" />
                                </div>
                                <div>
                                    <DialogTitle className="text-xl font-black text-foreground uppercase tracking-tight italic">{t("layout.soul_studio.create_modal_title")}</DialogTitle>
                                    <DialogDescription className="text-xs font-bold text-muted-foreground uppercase tracking-widest">{t("layout.soul_studio.create_modal_description")}</DialogDescription>
                                </div>
                            </div>
                            <button onClick={() => setIsCreateModalOpen(false)} aria-label={t("layout.soul_studio.close")} className="p-2 hover:bg-background rounded-full transition-all text-muted-foreground hover:text-foreground"><X className="w-6 h-6"/></button>
                        </div>
                        
                        {/* Body — 分區佈局（比照模型工坊自訂模型版型：身分 → 系統人格 → 推論參數/能力 → 安全設定） */}
                        <div className="flex-1 overflow-y-auto p-8 custom-scrollbar relative z-10 space-y-8">
                            {/* Section: Identity */}
                            <div className="space-y-4">
                                <h4 className="text-xs font-black flex items-center gap-2 text-muted-foreground uppercase tracking-widest border-b border-border pb-2">
                                    <Box className="w-4 h-4 text-primary" /> {t("layout.soul_studio.section_identity")}
                                </h4>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                    <div className="space-y-2">
                                        <label className="text-xs font-black text-muted-foreground uppercase tracking-widest ml-1">{t("layout.soul_studio.unique_core_id_label")}</label>
                                        <input
                                            className="w-full bg-background border border-input rounded-xl px-4 py-3 text-sm font-bold text-foreground outline-none focus:ring-2 focus:ring-primary/20 transition-all"
                                            placeholder={t("layout.soul_studio.unique_core_id_placeholder")}
                                            value={creationForm.id}
                                            onChange={e => setCreationForm({...creationForm, id: e.target.value})}
                                        />
                                    </div>
                                    <div className="space-y-2">
                                        <label className="text-xs font-black text-muted-foreground uppercase tracking-widest ml-1">{t("layout.soul_studio.display_name_label")}</label>
                                        <input
                                            className="w-full bg-background border border-input rounded-xl px-4 py-3 text-sm font-bold text-foreground outline-none focus:ring-2 focus:ring-primary/20 transition-all"
                                            placeholder={t("layout.soul_studio.display_name_placeholder")}
                                            value={creationForm.name}
                                            onChange={e => setCreationForm({...creationForm, name: e.target.value})}
                                        />
                                    </div>
                                </div>
                            </div>

                            {/* Section: System Persona (full width) */}
                            <div className="space-y-4">
                                <h4 className="text-xs font-black flex items-center gap-2 text-muted-foreground uppercase tracking-widest border-b border-border pb-2">
                                    <Cpu className="w-4 h-4 text-primary" /> {t("layout.soul_studio.section_system_persona")}
                                </h4>
                                <SystemPromptEditor
                                    value={creationForm.persona}
                                    onChange={val => setCreationForm({...creationForm, persona: val})}
                                    title={t("layout.soul_studio.persona_editor_title")}
                                />
                            </div>

                            {/* Section: Inference params | Abilities (2-col，比照模型工坊) */}
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
                                <div className="space-y-4">
                                    <h4 className="text-xs font-black flex items-center gap-2 text-muted-foreground uppercase tracking-widest border-b border-border pb-2">
                                        <Brain className="w-4 h-4 text-primary" /> {t("layout.soul_studio.section_inference_agent")}
                                    </h4>
                                    <div className="space-y-4">
                                        <div className="space-y-2">
                                            <label className="text-xs font-black text-muted-foreground uppercase tracking-widest ml-1">{t("layout.soul_studio.select_base_engine_label")}</label>
                                            {availableBaseModels.length > 0 ? (
                                                <select
                                                    className="w-full bg-background border border-input rounded-xl px-4 py-3 text-sm font-bold text-foreground outline-none focus:ring-2 focus:ring-primary/20 transition-all appearance-none cursor-pointer"
                                                    value={creationForm.baseModel}
                                                    onChange={e => setCreationForm({...creationForm, baseModel: e.target.value})}
                                                >
                                                    {availableBaseModels.map(id => <option key={id} value={id}>{id}</option>)}
                                                </select>
                                            ) : (
                                                <div className="px-4 py-3 bg-status-error/10 border border-status-error/20 rounded-xl text-status-error text-xs font-bold flex items-center gap-2 animate-pulse">
                                                    <Info size={14} /> {t("layout.soul_studio.no_models_available")}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                </div>

                                <div className="space-y-4">
                                    <h4 className="text-xs font-black flex items-center gap-2 text-muted-foreground uppercase tracking-widest border-b border-border pb-2">
                                        <Wrench className="w-4 h-4 text-primary" /> {t("layout.soul_studio.section_abilities_tools")}
                                    </h4>
                                    <div className="flex flex-wrap gap-2">
                                        {dynamicTools.map(tool => (
                                            <button
                                                key={tool.name}
                                                onClick={() => {
                                                    const tools = [...creationForm.enabledTools];
                                                    setCreationForm({
                                                        ...creationForm,
                                                        enabledTools: tools.includes(tool.name) ? tools.filter(existing => existing !== tool.name) : [...tools, tool.name]
                                                    });
                                                }}
                                                className={`px-4 py-2 rounded-xl text-xs font-bold border transition-all ${
                                                    creationForm.enabledTools.includes(tool.name)
                                                        ? "border-primary bg-primary/10 text-primary shadow-sm"
                                                        : "border-border bg-background text-muted-foreground hover:border-primary/30"
                                                }`}
                                            >
                                                {tool.name}
                                            </button>
                                        ))}
                                    </div>
                                </div>
                            </div>

                            {/* Section: Safety & Content (full width, SSOT 共用元件) */}
                            <SafetySettingsGrid
                                value={creationForm.safetySettings || []}
                                onChange={(next) => setCreationForm({ ...creationForm, safetySettings: next })}
                            />
                        </div>
                        
                        {/* Footer */}
                        <div className="p-8 border-t border-border flex justify-end gap-3 bg-muted/30">
                            <button 
                                onClick={() => setIsCreateModalOpen(false)}
                                className="px-6 py-3 rounded-xl text-xs font-bold text-muted-foreground hover:bg-background transition-all uppercase tracking-widest"
                            >
                                {t("layout.soul_studio.cancel")}
                            </button>
                            <button 
                                onClick={handleCreateSoul}
                                disabled={!creationForm.id.trim() || availableBaseModels.length === 0 || isLoading}
                                className="px-10 py-3 bg-primary-button text-white rounded-xl font-black uppercase tracking-widest text-xs hover:opacity-90 disabled:opacity-50 flex items-center gap-2 shadow-xl shadow-primary/20 transition-all transform active:scale-95"
                            >
                                {isLoading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                                {t("layout.soul_studio.initialize_soul")}
                            </button>
                        </div>
                    </DialogContent>
                </Dialog>
            )}

            {/* C4：步升密碼確認 modal（編輯儲存與刪除共用）*/}
            <Dialog open={pendingAction !== null} onOpenChange={(o) => { if (!o) closeConfirm(); }}>
                <DialogContent className="max-w-md" aria-busy={busy} onCloseAutoFocus={event => {
                    if (feedback.phase === 'saved') {
                        event.preventDefault();
                        editButton.current?.focus({ preventScroll: true });
                    }
                }}>
                    <DialogTitle className={`flex items-center gap-2 text-base font-black ${pendingAction === "delete" ? "text-status-error" : "text-foreground"}`}>
                        <ShieldAlert className="w-5 h-5" />
                        {pendingAction === "delete"
                            ? t("layout.soul_studio.delete_confirm_title", { id: activeSoul?.id ?? "" })
                            : t("layout.soul_studio.save_confirm_title")}
                    </DialogTitle>
                    <DialogDescription className="text-xs leading-relaxed">
                        {pendingAction === "delete"
                            ? t("layout.soul_studio.delete_confirm_body")
                            : t("layout.soul_studio.save_confirm_body")}
                    </DialogDescription>
                    {pendingAction === "delete" && <BackupRetentionNotice variant="delete" agentId={activeSoul?.id ?? ""} />}
                    <input
                        ref={passwordInput}
                        type="password"
                        autoFocus
                        disabled={busy}
                        aria-label={t("layout.soul_studio.stepup_password_placeholder")}
                        aria-invalid={pendingAction === 'save' && feedback.phase === 'error' && feedback.passwordError}
                        aria-describedby={pendingAction === 'save' && (feedback.phase === 'error' || feedback.phase === 'unknown') ? feedbackId : undefined}
                        value={confirmPassword}
                        onChange={(e) => setConfirmPassword(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter" && confirmPassword && !busy) { e.preventDefault(); void confirmAction(); } }}
                        placeholder={t("layout.soul_studio.stepup_password_placeholder")}
                        className="mt-2 w-full rounded-xl border border-input bg-muted/30 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/30"
                    />
                    {busy && <p>{t("layout.soul_studio.processing")}</p>}
                    {pendingAction === 'save' && feedback.phase === 'error' && (
                        <p id={feedbackId} role="alert">
                            {feedback.fieldError ? t("layout.soul_studio.invalid_fields") : errorMsg(feedback.code)}
                        </p>
                    )}
                    {pendingAction === 'save' && feedback.phase === 'unknown' && (
                        <div id={feedbackId} role="alert">
                            <p>{t("layout.soul_studio.save_unknown")}</p>
                            {reconciliation}
                            {feedback.refreshFailed && <p>{t("layout.soul_studio.read_failed")}</p>}
                            <button onClick={() => void soulSave.refresh()} disabled={feedback.refreshing}>
                                {t("layout.soul_studio.read_again")}
                            </button>
                        </div>
                    )}
                    <div className="mt-1 flex justify-end gap-2">
                        <button
                            onClick={closeConfirm}
                            disabled={busy}
                            className="rounded-xl px-4 py-2 text-xs font-bold text-muted-foreground hover:text-foreground uppercase tracking-widest"
                        >
                            {t("layout.soul_studio.cancel")}
                        </button>
                        <button
                            onClick={confirmAction}
                            disabled={!confirmPassword || busy || (pendingAction === 'save' && (unresolved || feedback.refreshing))}
                            className={`flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-black uppercase tracking-widest transition-all ${
                                !confirmPassword || busy
                                    ? "bg-muted text-muted-foreground cursor-not-allowed"
                                    : pendingAction === "delete"
                                        ? "bg-destructive text-white hover:opacity-90"
                                        : "bg-primary-button text-white hover:opacity-90"
                            }`}
                        >
                            {busy ? <Loader2 size={14} className="animate-spin" /> : (pendingAction === "delete" ? <Trash2 size={14} /> : <Check size={14} />)}
                            {pendingAction === "delete"
                                ? t("layout.soul_studio.confirm_delete")
                                : t("layout.soul_studio.confirm_save")}
                        </button>
                    </div>
                </DialogContent>
            </Dialog>
        </div>
    );
}
