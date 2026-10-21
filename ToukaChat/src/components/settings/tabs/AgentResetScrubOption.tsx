"use client";

import { useTranslation } from "react-i18next";
import { ShieldAlert } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";

/** 「一併清除備份裡的記憶」的選擇：勾選，以及第二段確認. */
export interface ScrubChoice {
    checked: boolean;
    acknowledged: boolean;
}

export const NO_SCRUB: ScrubChoice = { checked: false, acknowledged: false };

interface AgentResetScrubOptionProps {
    value: ScrubChoice;
    onChange: (next: ScrubChoice) => void;
    disabled?: boolean;
}

/**
 * 重置對話框裡的清除連備份選項：預設不勾；勾了才展開說明與第二段確認，
 * 按下確認才算數；取消勾選就整個歸零（關閉對話框由父元件歸零）。
 */
export function AgentResetScrubOption({ value, onChange, disabled }: AgentResetScrubOptionProps) {
    const { t } = useTranslation();

    return (
        <div className="space-y-2 rounded-xl border border-status-error/30 bg-status-error/5 p-2 text-xs">
            <label className="flex items-start gap-2 font-bold text-foreground">
                <Checkbox
                    checked={value.checked}
                    disabled={disabled}
                    onCheckedChange={(state) => onChange(state === true ? { checked: true, acknowledged: false } : NO_SCRUB)}
                />
                <span>{t("settings.agent_reset.scrub.option_label")}</span>
            </label>
            {value.checked && (
                <div className="space-y-1.5 text-muted-foreground">
                    <p>
                        <span className="font-black text-status-error">{t("settings.agent_reset.scrub.will_clear_title")}</span>{" "}
                        {t("settings.agent_reset.scrub.will_clear")}
                    </p>
                    <p>
                        <span className="font-black text-status-success">{t("settings.agent_reset.scrub.wont_clear_title")}</span>{" "}
                        {t("settings.agent_reset.scrub.wont_clear")}
                    </p>
                    <p>{t("settings.agent_reset.scrub.guarantee")}</p>
                    <p>{t("settings.agent_reset.scrub.duration")}</p>
                    {value.acknowledged ? (
                        <p className="flex items-center gap-1 font-bold text-status-error">
                            <ShieldAlert size={12} /> {t("settings.agent_reset.scrub.acknowledged")}
                        </p>
                    ) : (
                        <button
                            type="button"
                            disabled={disabled}
                            onClick={() => onChange({ checked: true, acknowledged: true })}
                            className="rounded-xl border border-status-error/30 bg-status-error/10 px-3 py-1.5 font-bold text-status-error transition-all hover:bg-status-error/20"
                        >
                            {t("settings.agent_reset.scrub.acknowledge")}
                        </button>
                    )}
                </div>
            )}
        </div>
    );
}
