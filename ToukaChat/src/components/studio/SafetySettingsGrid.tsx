"use client";

import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

interface SafetySetting {
    category: string;
    threshold: string;
}

const DEFAULT_THRESHOLD = "BLOCK_MEDIUM_AND_ABOVE";

/**
 * 安全與內容過濾設定格（SSOT 共用純呈現元件）。
 *
 * L2 自訂模型（ModelStudio）與 L1 靈魂「建立」表單（SoulStudio）共用同一實作，
 * 消除重複的安全設定 JSX。刻意無自帶狀態：`value` 進、`onChange` 出，
 * 由呼叫端持有真實狀態（草稿或既有設定）。
 */
export function SafetySettingsGrid({
    value,
    onChange,
}: {
    value: SafetySetting[];
    onChange: (next: SafetySetting[]) => void;
}) {
    const { t } = useTranslation();

    const categories = [
        { id: "HARM_CATEGORY_HARASSMENT", label: t("layout.model_studio.safety.harassment.label"), desc: t("layout.model_studio.safety.harassment.desc") },
        { id: "HARM_CATEGORY_HATE_SPEECH", label: t("layout.model_studio.safety.hate_speech.label"), desc: t("layout.model_studio.safety.hate_speech.desc") },
        { id: "HARM_CATEGORY_SEXUALLY_EXPLICIT", label: t("layout.model_studio.safety.sexually_explicit.label"), desc: t("layout.model_studio.safety.sexually_explicit.desc") },
        { id: "HARM_CATEGORY_DANGEROUS_CONTENT", label: t("layout.model_studio.safety.dangerous_content.label"), desc: t("layout.model_studio.safety.dangerous_content.desc") },
    ];

    const thresholds = [
        { id: "BLOCK_NONE", label: t("layout.model_studio.safety.threshold.none"), color: "hover:text-muted-foreground" },
        { id: "BLOCK_ONLY_HIGH", label: t("layout.model_studio.safety.threshold.relaxed"), color: "hover:text-status-warning" },
        { id: "BLOCK_MEDIUM_AND_ABOVE", label: t("layout.model_studio.safety.threshold.standard"), color: "hover:text-status-warning" },
        { id: "BLOCK_LOW_AND_ABOVE", label: t("layout.model_studio.safety.threshold.strict"), color: "hover:text-status-error" },
    ];

    const selectThreshold = (categoryId: string, thresholdId: string) => {
        const next = [...value];
        const idx = next.findIndex(s => s.category === categoryId);
        if (idx !== -1) next[idx] = { ...next[idx], threshold: thresholdId };
        else next.push({ category: categoryId, threshold: thresholdId });
        onChange(next);
    };

    return (
        <div className="space-y-6 pt-4 border-t border-border">
            <div className="flex items-center justify-between">
                <h3 className="text-xs font-black flex items-center gap-2 text-foreground uppercase tracking-widest text-muted-foreground">
                    <span className="material-symbols-outlined text-primary text-xl">verified_user</span> {t("layout.model_studio.section.safety_content")}
                </h3>
                <span className="text-xs font-bold px-2 py-0.5 bg-primary/10 text-primary rounded border border-primary/20 flex items-center gap-1">
                    {t("layout.model_studio.shields_enabled")}
                    <span className="text-xs opacity-70">{t("layout.model_studio.shields_gemini_note")}</span>
                </span>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-12 gap-y-6 bg-muted/20 p-6 rounded-2xl border border-border/50">
                {categories.map(cat => {
                    const currentThreshold = value.find(s => s.category === cat.id)?.threshold || DEFAULT_THRESHOLD;
                    return (
                        <div key={cat.id} className="space-y-3">
                            <div className="flex flex-col gap-1">
                                <label className="text-xs font-bold text-foreground">{cat.label}</label>
                                <p className="text-xs text-muted-foreground leading-tight">{cat.desc}</p>
                            </div>
                            <div className="flex p-1 bg-background rounded-xl border border-border shadow-sm">
                                {thresholds.map(level => {
                                    const isActive = currentThreshold === level.id;
                                    return (
                                        <button
                                            key={level.id}
                                            type="button"
                                            onClick={() => selectThreshold(cat.id, level.id)}
                                            className={cn(
                                                "flex-1 py-1.5 rounded-lg text-xs font-bold transition-all px-2 whitespace-nowrap",
                                                isActive
                                                    ? "bg-primary-button text-white shadow-md shadow-primary/20"
                                                    : `text-muted-foreground ${level.color} hover:bg-muted/50`
                                            )}
                                        >
                                            {level.label}
                                        </button>
                                    );
                                })}
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
