// src/components/settings/tabs/ThemeSettingsTab.tsx
"use client";

import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { Palette, Type, Check, Monitor, Sun, Moon, Clock, AlignJustify, MessageSquare, Sparkles } from "lucide-react";
import { useTheme as useNextTheme } from "next-themes";
import { useTheme, themes, ThemeColor } from "@/lib/ThemeContext";
import { Slider } from "@/components/ui/slider";
import { formatToTaipeiTime } from "@/lib/time_utils";
import { ThemeShowcase } from "./theme/ThemeShowcase";

// 固定範例時間戳（下午時段，凸顯 12/24 制差異）；用 epoch 數字避免於元件內呼叫 new Date（時間主權守衛 #3）。
const SAMPLE_EPOCH = 1_735_968_300_000;

interface SegOption { key: string; label: string }

function Segmented({ options, value, onChange }: { options: SegOption[]; value: string; onChange: (key: string) => void }) {
    return (
        <div className={cn("grid gap-2", options.length === 2 ? "grid-cols-2" : "grid-cols-3")}>
            {options.map((o) => (
                <button
                    key={o.key}
                    onClick={() => onChange(o.key)}
                    aria-pressed={value === o.key}
                    className={cn(
                        "flex items-center justify-center gap-1.5 p-3 rounded-2xl border transition-all text-xs font-bold",
                        value === o.key
                            ? "border-primary bg-primary/5 ring-2 ring-primary/20 text-primary"
                            : "border-border hover:border-primary/30 bg-card text-muted-foreground hover:text-foreground"
                    )}
                >
                    {o.label}
                </button>
            ))}
        </div>
    );
}

export function ThemeSettingsTab() {
    const { t } = useTranslation();
    const {
        themeColor, setThemeColor, fontSize, setFontSize,
        timeFormat, setTimeFormat, density, setDensity,
        bubbleStyle, setBubbleStyle, reduceMotion, setReduceMotion,
    } = useTheme();
    const previewCompact = density === 'compact';
    const previewSquare = bubbleStyle === 'square';
    // mode 軸（亮/暗/跟隨系統）由 next-themes 管（ADR-0105）；與上方 accent 軸正交。
    const { theme: mode, setTheme: setMode } = useNextTheme();
    const MODES = [
        { key: 'system', Icon: Monitor, label: t('settings.theme_settings_tab.mode_system') },
        { key: 'light', Icon: Sun, label: t('settings.theme_settings_tab.mode_light') },
        { key: 'dark', Icon: Moon, label: t('settings.theme_settings_tab.mode_dark') },
    ];

    return (
        <div className="flex flex-col xl:flex-row gap-8 animate-in fade-in slide-in-from-bottom-2 duration-300">
            {/* Left Column: Configuration */}
            <div className="w-full xl:w-80 space-y-8 shrink-0">
                <section className="space-y-4">
                    <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                        <Monitor className="w-4 h-4 text-primary" /> {t('settings.theme_settings_tab.theme_mode_heading')}
                    </h4>
                    <div className="grid grid-cols-3 gap-2">
                        {MODES.map(({ key, Icon, label }) => (
                            <button
                                key={key}
                                onClick={() => setMode?.(key)}
                                aria-pressed={mode === key}
                                className={cn(
                                    "flex flex-col items-center gap-1.5 p-3 rounded-2xl border transition-all",
                                    mode === key
                                        ? "border-primary bg-primary/5 ring-2 ring-primary/20 text-primary"
                                        : "border-border hover:border-primary/30 bg-card text-muted-foreground hover:text-foreground"
                                )}
                            >
                                <Icon className="w-4 h-4" />
                                <span className="text-xs font-bold">{label}</span>
                            </button>
                        ))}
                    </div>
                </section>

                <section className="space-y-4">
                    <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                        <Palette className="w-4 h-4 text-primary" /> {t('settings.theme_settings_tab.theme_heading')}
                    </h4>
                    <p className="text-xs text-muted-foreground leading-relaxed">
                        {t('settings.theme_settings_tab.theme_description')}
                    </p>
                    <div className="grid grid-cols-2 gap-3 pb-2">
                        {(Object.keys(themes) as ThemeColor[]).map((key) => (
                            <button 
                                key={key} 
                                onClick={() => setThemeColor(key)}
                                className={cn(
                                    "flex items-center gap-3 p-3 rounded-2xl border transition-all relative overflow-hidden group",
                                    themeColor === key 
                                        ? "border-primary bg-primary/5 ring-2 ring-primary/20" 
                                        : "border-border hover:border-primary/30 bg-card"
                                )}
                            >
                                <div 
                                    className="w-6 h-6 rounded-full shrink-0 shadow-inner" 
                                    style={{ backgroundColor: themes[key].primary }} 
                                />
                                <span className={cn("text-xs font-bold", themeColor === key ? "text-primary" : "text-muted-foreground group-hover:text-foreground")}>
                                    {/* 主題名走 i18n：以主題 key 組 `theme.names.<key>`，design SSOT tokens.ts 不動。
                                        動態鍵 i18next-parser 靜態掃不到 → Phase 3 守衛 #33 應對每個 THEMES key 驗 theme.names.<key> 存在。 */}
                                    {t(`theme.names.${key}`)}
                                </span>
                                {themeColor === key && (
                                    <div className="absolute top-2 right-2">
                                        <Check className="w-3 h-3 text-primary" />
                                    </div>
                                )}
                            </button>
                        ))}
                    </div>
                </section>

                <section className="space-y-4">
                    <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                        <Type className="w-4 h-4 text-primary" /> {t('settings.theme_settings_tab.font_size_heading', { size: fontSize })}
                    </h4>
                    <div className="flex items-center gap-4 bg-muted/30 p-5 rounded-2xl border border-border">
                        <span className="shrink-0 text-xs font-bold uppercase tracking-widest text-muted-foreground">{t('settings.theme_settings_tab.font_size_small')}</span>
                        <Slider
                            min={12} 
                            max={24} 
                            step={1}
                            value={[fontSize]} 
                            onValueChange={(val) => setFontSize(val[0])}
                            className="flex-1"
                        />
                        <span className="shrink-0 text-xs font-bold uppercase tracking-widest text-muted-foreground">{t('settings.theme_settings_tab.font_size_large')}</span>
                    </div>
                </section>

                <section className="space-y-4">
                    <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                        <Clock className="w-4 h-4 text-primary" /> {t('settings.theme_settings_tab.time_format_heading')}
                    </h4>
                    <Segmented
                        value={timeFormat}
                        onChange={(v) => setTimeFormat(v as typeof timeFormat)}
                        options={[
                            { key: '12h', label: t('settings.theme_settings_tab.time_format_12h') },
                            { key: '24h', label: t('settings.theme_settings_tab.time_format_24h') },
                        ]}
                    />
                    <p className="text-xs text-muted-foreground">
                        {t('settings.theme_settings_tab.time_format_example')}：
                        <span className="font-mono font-bold text-foreground">
                            {formatToTaipeiTime(SAMPLE_EPOCH, { hour: 'numeric', minute: 'numeric', second: 'numeric', hour12: timeFormat === '12h' })}
                        </span>
                    </p>
                </section>

                <section className="space-y-4">
                    <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                        <AlignJustify className="w-4 h-4 text-primary" /> {t('settings.theme_settings_tab.density_heading')}
                    </h4>
                    <Segmented
                        value={density}
                        onChange={(v) => setDensity(v as typeof density)}
                        options={[
                            { key: 'comfortable', label: t('settings.theme_settings_tab.density_comfortable') },
                            { key: 'compact', label: t('settings.theme_settings_tab.density_compact') },
                        ]}
                    />
                </section>

                <section className="space-y-4">
                    <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                        <MessageSquare className="w-4 h-4 text-primary" /> {t('settings.theme_settings_tab.bubble_style_heading')}
                    </h4>
                    <Segmented
                        value={bubbleStyle}
                        onChange={(v) => setBubbleStyle(v as typeof bubbleStyle)}
                        options={[
                            { key: 'rounded', label: t('settings.theme_settings_tab.bubble_rounded') },
                            { key: 'square', label: t('settings.theme_settings_tab.bubble_square') },
                        ]}
                    />
                    {/* 即時泡泡預覽：套用與聊天室相同的密度（間距/內距）＋泡泡樣式（圓角/方角）邏輯。 */}
                    <div
                        data-testid="bubble-preview"
                        className={cn("flex flex-col rounded-2xl border border-border bg-sidebar/50 p-4", previewCompact ? "gap-2" : "gap-4")}
                    >
                        <div className={cn(
                            "self-end max-w-[85%] text-xs shadow-sm bg-primary-button text-white",
                            previewSquare ? "rounded-md" : "rounded-2xl rounded-tr-none",
                            previewCompact ? "px-3 py-2" : "px-4 py-3"
                        )}>
                            {t('settings.theme_settings_tab.bubble_preview_user')}
                        </div>
                        <div className={cn(
                            "self-start max-w-[85%] text-xs text-foreground shadow-sm bg-muted/30 border border-border",
                            previewSquare ? "rounded-md" : "rounded-2xl rounded-tl-none",
                            previewCompact ? "px-3 py-2" : "px-4 py-3"
                        )}>
                            {t('settings.theme_settings_tab.bubble_preview_assistant')}
                        </div>
                    </div>
                </section>

                <section className="space-y-4">
                    <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                        <Sparkles className="w-4 h-4 text-primary" /> {t('settings.theme_settings_tab.reduce_motion_heading')}
                    </h4>
                    <p className="text-xs text-muted-foreground leading-relaxed">
                        {t('settings.theme_settings_tab.reduce_motion_description')}
                    </p>
                    <Segmented
                        value={reduceMotion}
                        onChange={(v) => setReduceMotion(v as typeof reduceMotion)}
                        options={[
                            { key: 'system', label: t('settings.theme_settings_tab.reduce_motion_system') },
                            { key: 'on', label: t('settings.theme_settings_tab.reduce_motion_on') },
                            { key: 'off', label: t('settings.theme_settings_tab.reduce_motion_off') },
                        ]}
                    />
                </section>
            </div>

            {/* Right Column: Live Showcase */}
            <div className="flex-1 space-y-4">
                <div className="flex items-center justify-between">
                    <h4 className="text-xs font-black uppercase tracking-widest text-muted-foreground">{t('settings.theme_settings_tab.live_preview_heading')}</h4>
                </div>
                <div className="min-h-96 rounded-3xl border border-border bg-sidebar/50 p-6 shadow-inner">
                   <ThemeShowcase />
                </div>
            </div>
        </div>
    );
}
