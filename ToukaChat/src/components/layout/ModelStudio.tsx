// src/components/layout/ModelStudio.tsx
// 模型工坊 (Model Workshop) — 職責：僅管理 Level 2 自訂模型 (custom models)
// Level 1 靈魂 (souls) 的管理由靈魂核心頁面 (SoulStudio) 負責。
"use client";

import { useState, useEffect, useCallback } from "react";
import { cn } from "@/lib/utils";
import {
    Save, Trash2, Box, Wrench,
    PlusCircle,
} from "lucide-react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { getSystemEpoch } from "@/lib/time_utils";
import { Model } from "@/lib/types";
import { isSelectableBaseModel } from "@/lib/default_model";
import { SafetySettingsGrid } from "@/components/studio/SafetySettingsGrid";
import { SystemPromptEditor } from "@/components/studio/SystemPromptEditor";
import { logSSOTError, getErrorMessage, getCollisionMessage, errorMsg } from "@/lib/error_handler";
import { toast } from "sonner";
import { useLocalStorage } from "@/hooks/useLocalStorage";
import { useTranslation } from "react-i18next";

interface CustomModel {
    id: string;
    originalId?: string; // 用於追蹤改名
    name: string;
    description: string;
    systemPrompt: string;
    baseModel: string;
    temperature: number;
    topP: number;
    topK: number;
    enabledTools: string[];
    safetySettings?: Array<{ category: string; threshold: string }>;
}

const DEFAULT_MODEL_CONFIG: CustomModel = {
    id: "",
    name: "新模型",
    description: "自定義 AI 模型設定",
    systemPrompt: "你是一個專業的 AI 助手。",
    baseModel: "", // 空值：顯示與存檔時用清單第一個一般模型（ADR-0250）
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

function toModelId(name: string): string {
    const slug = name.trim().replace(/\s+/g, '_').replace(/[^a-zA-Z0-9_\u4e00-\u9fa5]/g, '');
    return slug || `custom-${getSystemEpoch()}`;
}

export function ModelStudio() {
    const { t } = useTranslation();
    const [models, setModels] = useState<CustomModel[]>([]);
    const [activeModelId, setActiveModelId] = useLocalStorage<string | null>("touka_studio_active_id", null);
    const [availableBaseModels, setAvailableBaseModels] = useState<string[]>([]);
    const [dynamicTools, setDynamicTools] = useState<{ name: string, desc: string, category: string }[]>([]);
    const [, setIsLoading] = useState(false);

    const loadModels = useCallback(async () => {
        setIsLoading(true);
        try {
            const proxyUrl = getActiveProxyUrl();

            // 基礎引擎選單：只列一般模型（規則和靈魂頁共用，見 isSelectableBaseModel）
            const mRes = await fetchTouka(proxyUrl, "/v1/models");
            if (mRes.ok) {
                const data = await mRes.json();
                if (data.data) {
                    const ids = (data.data as Model[])
                        .filter(isSelectableBaseModel)
                        .map((m: Model) => m.id);
                    setAvailableBaseModels(ids);
                }
            }

            // 自定義模型 (Level 2 only) — SSOT 原則
            // 模型工坊職責邊界：僅管理 /ui/custom_models，靈魂由靈魂核心管理
            const customRes = await fetchTouka(proxyUrl, "/v1/ui/custom_models");
            let customModels: CustomModel[] = [];
            if (customRes.ok) {
                const customData = await customRes.json();
                customModels = (customData.models || []).map((m: { id: string, name: string, description: string, systemPrompt: string, config: Record<string, unknown> }) => ({
                    id: m.id,
                    originalId: m.id,
                    name: m.name,
                    description: m.description,
                    systemPrompt: m.systemPrompt,
                    baseModel: (m.config?.base_model as string) || "",
                    temperature: (m.config?.temperature as number) ?? 0.7,
                    topP: (m.config?.top_p as number) ?? 1.0,
                    topK: (m.config?.top_k as number) ?? 40,
                    enabledTools: (m.config?.enabled_tools as string[]) || [],
                    safetySettings: (m.config?.safety_settings as Array<{ category: string; threshold: string }>) || [...DEFAULT_MODEL_CONFIG.safetySettings!]
                }));
            }

            setModels(customModels.length > 0 ? customModels : [{ ...DEFAULT_MODEL_CONFIG, id: `custom-${getSystemEpoch()}` }]);

            if (customModels.length > 0) {
                if (!activeModelId || !customModels.find(m => m.id === activeModelId)) {
                    setActiveModelId(customModels[0].id);
                }
            }
        } catch (err) {
            logSSOTError(err, "ModelStudio:loadModels");
            setModels([{ ...DEFAULT_MODEL_CONFIG, id: `fallback-${getSystemEpoch()}` }]);
        }
        setIsLoading(false);
    }, [activeModelId, setActiveModelId]);

    useEffect(() => {
        const timer = setTimeout(() => {
            loadModels();
            const fetchTools = async () => {
                try {
                    const proxyUrl = getActiveProxyUrl();
                    const res = await fetchTouka(proxyUrl, "/v1/tools_stats");
                    if (res.ok) {
                        const data = await res.json();
                        if (data.tools) setDynamicTools(data.tools);
                    }
                } catch { }
            };
            fetchTools();
        }, 0);
        return () => clearTimeout(timer);
    }, [loadModels]);

    if (!models.length) {
        return (
            <div className="flex h-full w-full items-center justify-center bg-background/50 backdrop-blur-sm">
                <div className="flex flex-col items-center gap-4">
                    <div className="w-10 h-10 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
                    <p className="text-sm font-bold text-muted-foreground animate-pulse">{t("layout.model_studio.loading")}</p>
                </div>
            </div>
        );
    }

    const activeModel = models.find(m => m.id === activeModelId) || models[0];
    // 沒有基礎引擎時用清單第一個一般模型（ADR-0250）；畫面與存檔用同一個值。
    const baseModel = activeModel.baseModel || availableBaseModels[0] || "";

    const saveModel = async () => {
        if (!activeModel) return;
        const proxyUrl = getActiveProxyUrl();

        try {
            // 職責邊界：ModelStudio 只儲存 Level 2 自訂模型 (/ui/custom_models)
            const targetId = toModelId(activeModel.name);

            // 如果有原始 ID 且與目標 ID 不同，先執行更名邏輯
            if (activeModel.originalId && activeModel.originalId !== targetId) {
                const renameRes = await fetchTouka(proxyUrl, `/v1/ui/custom_models/${activeModel.originalId}/rename`, {
                    method: "POST",
                    body: JSON.stringify({ new_id: targetId })
                });
                if (!renameRes.ok) {
                    if (renameRes.status === 409) {
                        toast.error((await getCollisionMessage(renameRes)) ?? errorMsg("MODEL_CONFLICT"));
                        return;
                    }
                    // 改名失敗就停下：再對新 ID 存檔會多出第二個模型（舊審查報告丙-2）
                    logSSOTError(new Error("Rename failed"), "ModelStudio:saveModel:rename");
                    toast.error(await getErrorMessage(renameRes, "CONFIG_SAVE_FAILED"));
                    return;
                }
                // 改名成功就記下後端的新 ID：接下來的存檔失敗時，重試不會再送一次舊→新的改名。
                // 不換畫面上的 ID：換了會重新載入清單，洗掉還沒存的修改（舊審查報告丙-2）
                setModels(prev => prev.map(m => (m.id === activeModel.id ? { ...m, originalId: targetId } : m)));
            }

            const saveRes = await fetchTouka(proxyUrl, `/v1/ui/custom_models/${targetId}`, {
                method: "POST",
                body: JSON.stringify({
                    name: activeModel.name,
                    description: activeModel.description,
                    systemPrompt: activeModel.systemPrompt,
                    config: {
                        base_model: baseModel,
                        temperature: activeModel.temperature,
                        top_p: activeModel.topP,
                        top_k: activeModel.topK,
                        enabled_tools: activeModel.enabledTools,
                        safety_settings: activeModel.safetySettings
                    }
                })
            });

            if (!saveRes.ok) {  // 建立時撞名等：說清楚原因，不說存好了
                toast.error((await getCollisionMessage(saveRes)) ?? (await getErrorMessage(saveRes, "CONFIG_SAVE_FAILED")));
                return;
            }

            if (targetId !== activeModel.id) {
                setActiveModelId(targetId);
            }

            // 通知系統刷新模型列表
            window.dispatchEvent(new CustomEvent("touka_refresh_models"));
            toast.success(t("layout.model_studio.toast.save_success"));
        } catch (err) {
            logSSOTError(err, "ModelStudio:saveModel");
            toast.error(await getErrorMessage(err, "CONFIG_SAVE_FAILED"));
        }
    };

    const deleteModel = async (id: string) => {
        // 送後端記著的 ID（改名成功、存檔失敗後畫面上的 id 還是舊的）；還沒存過的新模型只在畫面上拿掉。
        // 後端沒刪成就顯示錯誤、不從清單拿掉（舊審查報告丙-2）
        const savedId = models.find(m => m.id === id)?.originalId;
        try {
            if (savedId) {
                const proxyUrl = getActiveProxyUrl();
                // 職責邊界：ModelStudio 只刪除 Level 2 自訂模型
                const res = await fetchTouka(proxyUrl, `/v1/ui/custom_models/${savedId}`, { method: "DELETE" });
                // 404：已經被刪掉了（例如別的分頁），照樣從畫面拿掉
                if (!res.ok && res.status !== 404) {
                    toast.error(await getErrorMessage(res, "MODEL_DELETE_FAILED"));
                    return;
                }
                window.dispatchEvent(new CustomEvent("touka_refresh_models"));
            }
        } catch (err) {
            logSSOTError(err, "ModelStudio:deleteModel");
            toast.error(await getErrorMessage(err, "MODEL_DELETE_FAILED"));
            return;
        }

        const filtered = models.filter(m => m.id !== id);
        if (filtered.length === 0) {
            const fallbackModel = { ...DEFAULT_MODEL_CONFIG, id: "model-" + getSystemEpoch() };
            setModels([fallbackModel]);
            setActiveModelId(fallbackModel.id);
        } else {
            setModels(filtered);
            if (activeModelId === id) setActiveModelId(filtered[0].id);
        }
    };

    if (!activeModel) return null;

    return (
        <div className="flex flex-col h-full bg-background overflow-hidden font-sans">
            {/* Header */}
            <header className="h-16 border-b border-border flex items-center justify-between px-6 bg-sidebar sticky top-0 z-10">
                <div className="flex items-center gap-4 flex-1">
                    <div className="flex items-center gap-2 px-3 py-1.5 bg-muted/50 rounded-lg border border-border shadow-sm transition-all focus-within:ring-2 focus-within:ring-primary/20">
                        <Box className="w-5 h-5 text-primary" />
                        <select 
                            value={activeModelId || ""} 
                            onChange={(e) => setActiveModelId(e.target.value)} 
                            className="bg-transparent border-none text-base font-bold focus:ring-0 cursor-pointer w-40 text-foreground appearance-none"
                        >
                            {models.map(m => (
                                <option key={m.id} value={m.id} className="bg-popover text-foreground">
                                    {m.name}
                                </option>
                            ))}
                        </select>
                    </div>
                    <button onClick={() => {
                        // 新建立時給予暫時的唯一 ID，儲存時會再依據名稱轉換
                        const newId = `temp-${getSystemEpoch()}`;
                        const newM = { ...DEFAULT_MODEL_CONFIG, id: newId, name: t("layout.model_studio.new_custom_model_name") };
                        setModels([newM, ...models]);
                        setActiveModelId(newId);
                    }} className="p-2 hover:bg-background rounded-full transition-colors text-muted-foreground hover:text-primary" title={t("layout.model_studio.create_new_model")}><PlusCircle className="w-5 h-5" /></button>
                </div>
                
                <div className="flex items-center gap-3">
                    <button onClick={saveModel} className="flex items-center gap-2 px-5 py-2 bg-primary-button text-primary-foreground rounded-xl text-xs font-black uppercase tracking-widest hover:opacity-90 shadow-lg shadow-primary/20 transition-all"><Save className="w-4 h-4" /> Save Config</button>
                    <button onClick={() => deleteModel(activeModel.id)} className="p-2 text-muted-foreground hover:text-status-error hover:bg-status-error/10 rounded-xl transition-all"><Trash2 className="w-5 h-5" /></button>
                </div>
            </header>

            {/* Content Area */}
            <div className="flex-1 overflow-y-auto p-6 space-y-8 custom-scrollbar bg-background">
                <div className="grid grid-cols-1 md:grid-cols-3 gap-6 items-start">
                    <div className="space-y-2">
                        <label className="text-xs font-black uppercase text-muted-foreground tracking-widest ml-1">{t("layout.model_studio.field.model_name")}</label>
                        <input value={activeModel.name} onChange={e => {
                            const newModels = [...models];
                            const idx = newModels.findIndex(m => m.id === activeModel.id);
                            newModels[idx].name = e.target.value;
                            setModels(newModels);
                        }} className="w-full bg-muted/30 border border-input rounded-xl px-4 py-3 text-sm font-bold text-foreground focus:ring-2 focus:ring-primary/30 outline-none transition-all" />
                    </div>
                    <div className="space-y-2">
                        <label className="text-xs font-black uppercase text-muted-foreground tracking-widest ml-1">{t("layout.model_studio.field.description")}</label>
                        <input value={activeModel.description} onChange={e => {
                            const newModels = [...models];
                            const idx = newModels.findIndex(m => m.id === activeModel.id);
                            newModels[idx].description = e.target.value;
                            setModels(newModels);
                        }} className="w-full bg-muted/30 border border-input rounded-xl px-4 py-3 text-sm font-medium text-muted-foreground focus:ring-2 focus:ring-primary/30 outline-none transition-all" />
                    </div>
                    <div className="space-y-2">
                        <label className="text-xs font-black uppercase text-muted-foreground tracking-widest ml-1">{t("layout.model_studio.field.base_brain")}</label>
                        <select value={baseModel} onChange={e => {
                            const newModels = [...models];
                            const idx = newModels.findIndex(m => m.id === activeModel.id);
                            newModels[idx].baseModel = e.target.value;
                            setModels(newModels);
                        }} className="w-full bg-muted/30 border border-input rounded-xl px-4 py-3 text-sm font-bold text-foreground focus:ring-2 focus:ring-primary/30 outline-none transition-all">
                            {availableBaseModels.map(id => <option key={id} value={id} className="bg-popover text-foreground">{id}</option>)}
                        </select>
                    </div>
                </div>

                <SystemPromptEditor 
                    value={activeModel.systemPrompt} 
                    onChange={val => {
                        const newModels = [...models];
                        const idx = newModels.findIndex(m => m.id === activeModel.id);
                        newModels[idx].systemPrompt = val;
                        setModels(newModels);
                    }} 
                    title={t("layout.model_studio.system_prompt_title")}
                />

                {/* 取樣參數不會送出（ADR-0241），拉桿已拿掉；欄位照原值存回（ADR-0250） */}
                <div className="pt-4">
                    <div className="space-y-6">
                        <h3 className="text-xs font-black flex items-center gap-2 border-b border-border pb-2 text-foreground uppercase tracking-widest text-muted-foreground">
                            <Wrench className="w-4 h-4 text-primary" /> {t("layout.model_studio.section.capabilities")}
                        </h3>
                        <div className="space-y-4">
                            {["native", "memory", "plugin"].map(cat => {
                                const catTools = dynamicTools.filter(t => t.category === cat).map(t => t.name);
                                if (catTools.length === 0) return null;
                                return (
                                    <div key={cat} className="space-y-1.5">
                                        <div className="text-xs text-muted-foreground uppercase font-black tracking-widest ml-1">{cat}</div>
                                        <div className="flex flex-wrap gap-2">
                                            {catTools.map(tool => (
                                                <button key={tool} onClick={() => {
                                                    const newModels = [...models];
                                                    const idx = newModels.findIndex(m => m.id === activeModel.id);
                                                    const tools = [...newModels[idx].enabledTools];
                                                    if (tools.includes(tool)) newModels[idx].enabledTools = tools.filter(t => t !== tool);
                                                    else newModels[idx].enabledTools = [...tools, tool];
                                                    setModels(newModels);
                                                }} className={cn(
                                                    "px-3 py-1.5 rounded-xl text-xs font-bold border transition-all",
                                                    activeModel.enabledTools.includes(tool)
                                                        ? "border-primary bg-primary/10 text-primary"
                                                        : "border-border bg-sidebar/50 text-muted-foreground hover:border-primary/30"
                                                )}>{tool}</button>
                                            ))}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                </div>

                {/* Safety Settings Section — SSOT 共用元件（與 SoulStudio 建立表單同源） */}
                <SafetySettingsGrid
                    value={activeModel.safetySettings || []}
                    onChange={(next) => {
                        const newModels = [...models];
                        const idx = newModels.findIndex(m => m.id === activeModel.id);
                        newModels[idx].safetySettings = next;
                        setModels(newModels);
                    }}
                />
            </div>
        </div>
    );
}
