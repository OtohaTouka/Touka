"use client";

import { useTranslation } from "react-i18next";
import {
    isToolSelected,
    shortNames,
    toggleTool,
    unlistedEntries,
    type SoulToolEntry,
} from "@/lib/soul_tools";

interface SoulToolPickerProps {
    tools: readonly SoulToolEntry[];
    value: readonly string[];
    /** 沒給就是唯讀：只列出勾了的工具 */
    onChange?: (next: string[]) => void;
}

const CHIP = "px-3 py-1.5 rounded-xl text-xs font-bold border transition-all";
const ON = "border-primary bg-primary/10 text-primary shadow-sm";
const OFF = "border-border bg-background text-muted-foreground hover:border-primary/30";

/**
 * 靈魂的工具清單（建立與編輯共用，ADR-0281 T2）。
 * 清單裡找不到的條目（斷線伺服器的工具、你個人授權的工具、舊短名）照樣保留並標示，不會悄悄丟掉。
 */
export function SoulToolPicker({ tools, value, onChange }: SoulToolPickerProps) {
    const { t } = useTranslation();
    const unlisted = unlistedEntries(value, tools);
    const shortCount = shortNames(value, tools).length;
    const shown = onChange ? tools : tools.filter(entry => isToolSelected(entry, value));
    const notListed = t("layout.soul_studio.tools_not_listed");

    return (
        <div className="space-y-2">
            <div className="flex flex-wrap gap-1.5">
                {shown.map(entry => {
                    const selected = isToolSelected(entry, value);
                    return onChange ? (
                        <button
                            key={entry.key}
                            type="button"
                            aria-pressed={selected}
                            title={entry.description || undefined}
                            onClick={() => onChange(toggleTool(entry, value))}
                            className={`${CHIP} ${selected ? ON : OFF}`}
                        >
                            {entry.label}
                        </button>
                    ) : (
                        <span key={entry.key} title={entry.description || undefined} className={`${CHIP} ${OFF}`}>
                            {entry.label}
                        </span>
                    );
                })}
                {unlisted.map(item => onChange ? (
                    <button
                        key={item}
                        type="button"
                        aria-pressed
                        onClick={() => onChange(value.filter(existing => existing !== item))}
                        className={`${CHIP} ${ON} border-dashed`}
                    >
                        {item} · {notListed}
                    </button>
                ) : (
                    <span key={item} className={`${CHIP} ${OFF} border-dashed`}>
                        {item} · {notListed}
                    </span>
                ))}
            </div>
            {shortCount > 0 && (
                <p className="text-xs text-status-warning">
                    {t("layout.soul_studio.tools_needs_reselect", { count: shortCount })}
                </p>
            )}
            {onChange && (
                <p className="text-xs text-muted-foreground">{t("layout.soul_studio.tools_empty_means_all")}</p>
            )}
        </div>
    );
}
