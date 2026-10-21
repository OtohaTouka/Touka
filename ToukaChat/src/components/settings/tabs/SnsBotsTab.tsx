// src/components/settings/tabs/SnsBotsTab.tsx
"use client";

import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { MessageSquare, Trash2, Plus, Database } from "lucide-react";
import type { useSnsBotSettings } from "@/hooks/settings/useSnsBotSettings";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { AgentIdentity } from "@/lib/types";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";

interface Props {
    allAgents?: AgentIdentity[];
    settings: ReturnType<typeof useSnsBotSettings>;
}

// B2: 為新 bot 產生穩定 id（React key 與後端 token write-only 比對共用）。
// 不使用 Date.now()（時間主權守衛 #3）；crypto.randomUUID 為主、亂數為退路。
const newBotId = (): string =>
    typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `bot_${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;

export function SnsBotsTab({ allAgents = [], settings }: Props) {
    const { t } = useTranslation();
    const { snsBots, setSnsBots, saveSnsBots, fetchSnsBots, resetSnsDrafts, loadState, isSaving, tokenDraftCleared } = settings;
    const { confirmToast } = useConfirmToast();
    const disabled = isSaving || loadState !== 'ready';

    useEffect(() => {
        void fetchSnsBots();
        return resetSnsDrafts;
    }, [fetchSnsBots, resetSnsDrafts]);

    // 修改特定 bot 項目的欄位值
    const handleUpdateBot = (platform: "discord" | "telegram", index: number, field: string, value: string | boolean) => {
        const updated = { ...snsBots };
        updated[platform] = updated[platform].map((bot, i) => 
            i === index ? { ...bot, [field]: value } : bot
        );
        setSnsBots(updated);
    };

    // 新增 bot 項目
    const handleAddBot = (platform: "discord" | "telegram") => {
        const updated = { ...snsBots };
        const newBot = {
            id: newBotId(),
            token: "",
            agent_id: allAgents[0]?.id || "",
            enabled: true,
            session_mode: "independent",
            is_multi_agent: false
        };
        updated[platform] = [...updated[platform], newBot];
        setSnsBots(updated);
    };

    // 刪除 bot 項目
    const handleRemoveBot = (platform: "discord" | "telegram", index: number) => {
        const updated = { ...snsBots };
        updated[platform] = updated[platform].filter((_, i) => i !== index);
        setSnsBots(updated);
    };

    const handleSave = async () => {
        await saveSnsBots(snsBots);
    };

    const renderBotList = (platform: "discord" | "telegram") => {
        const list = snsBots[platform] || [];
        const title = platform === "discord" ? t('settings.sns_bots_tab.discord_bots_title') : t('settings.sns_bots_tab.telegram_bots_title');

        return (
            <div className="space-y-4">
                <div className="flex items-center justify-between">
                    <h5 className="text-sm font-bold text-foreground">{title}</h5>
                    <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleAddBot(platform)}
                        className="flex items-center gap-1.5 text-xs font-bold rounded-lg border-primary/20 bg-primary/5 text-primary hover:bg-primary/10"
                    >
                        <Plus className="w-3.5 h-3.5" />
                        {t('settings.sns_bots_tab.add_bot_button')}
                    </Button>
                </div>

                <div className="space-y-4">
                    {list.map((bot, index) => (
                        <div
                            key={bot.id ?? index}
                            className="p-5 rounded-2xl border border-border bg-card shadow-sm space-y-4 relative group"
                        >
                            {/* Trash Button */}
                            <Button
                                variant="ghost"
                                size="icon"
                                aria-label={t('settings.sns_bots_tab.remove_bot_aria', { num: index + 1, platform })}
                                onClick={() => confirmToast(t('settings.sns_bots_tab.confirm_remove_bot'), () => handleRemoveBot(platform, index))}
                                className="absolute top-4 right-4 text-muted-foreground hover:text-destructive rounded-lg transition-colors"
                            >
                                <Trash2 className="w-4 h-4" />
                            </Button>

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                {/* Bot Token */}
                                <div className="space-y-1.5">
                                    <label htmlFor={`sns-token-${platform}-${bot.id ?? index}`} className="text-xs font-bold text-muted-foreground">{t('settings.sns_bots_tab.bot_token_label')}</label>
                                    <Input
                                        id={`sns-token-${platform}-${bot.id ?? index}`}
                                        type="password"
                                        autoComplete="off"
                                        placeholder={t('settings.sns_bots_tab.bot_token_ph')}
                                        value={bot.token}
                                        onChange={(e) => handleUpdateBot(platform, index, "token", e.target.value)}
                                        className="rounded-lg border border-border bg-background text-xs"
                                    />
                                </div>

                                {/* Agent ID Select */}
                                <div className="space-y-1.5">
                                    <label htmlFor={`sns-agent-${platform}-${bot.id ?? index}`} className="text-xs font-bold text-muted-foreground">{t('settings.sns_bots_tab.bind_agent_id_label')}</label>
                                    {allAgents.length > 0 ? (
                                        <Select
                                            value={bot.agent_id}
                                            onValueChange={(val) => handleUpdateBot(platform, index, "agent_id", val)}
                                        >
                                            <SelectTrigger id={`sns-agent-${platform}-${bot.id ?? index}`} className="rounded-lg border border-border bg-background text-xs font-bold text-foreground">
                                                <SelectValue placeholder={t('settings.sns_bots_tab.select_agent_ph')} />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {allAgents.map((agent) => (
                                                    <SelectItem key={agent.id} value={agent.id} className="text-xs font-bold">
                                                        {agent.name} ({agent.id})
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    ) : (
                                        <Input
                                            id={`sns-agent-${platform}-${bot.id ?? index}`}
                                            type="text"
                                            placeholder={t('settings.sns_bots_tab.agent_id_ph')}
                                            value={bot.agent_id}
                                            onChange={(e) => handleUpdateBot(platform, index, "agent_id", e.target.value)}
                                            className="rounded-lg border border-border bg-background text-xs"
                                        />
                                    )}
                                </div>
                            </div>

                            <div className="flex flex-wrap gap-6 pt-2 items-center">
                                {/* Enabled Switch */}
                                <div className="flex items-center gap-2">
                                    <Switch
                                        checked={bot.enabled}
                                        onCheckedChange={(checked) => handleUpdateBot(platform, index, "enabled", checked)}
                                        aria-label={t('settings.sns_bots_tab.enable_bot')}
                                    />
                                    <span className="text-xs font-bold text-foreground">{t('settings.sns_bots_tab.enable_bot')}</span>
                                </div>

                                {/* Multi Agent Switch */}
                                <div className="flex items-center gap-2">
                                    <Switch
                                        checked={bot.is_multi_agent || false}
                                        onCheckedChange={(checked) => handleUpdateBot(platform, index, "is_multi_agent", checked)}
                                        aria-label={t('settings.sns_bots_tab.multi_agent_conversation')}
                                    />
                                    <span className="text-xs font-bold text-foreground">{t('settings.sns_bots_tab.multi_agent_conversation')}</span>
                                </div>

                                {/* Session Mode Select */}
                                <div className="flex items-center gap-2">
                                    <span className="text-xs font-bold text-muted-foreground">{t('settings.sns_bots_tab.session_mode_label')}</span>
                                    <Select
                                        value={bot.session_mode || "independent"}
                                        onValueChange={(val) => handleUpdateBot(platform, index, "session_mode", val)}
                                    >
                                        <SelectTrigger className="w-36 h-8 rounded-lg border border-border bg-background text-xs font-bold text-foreground">
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="independent" className="text-xs font-bold">{t('settings.sns_bots_tab.session_mode_independent')}</SelectItem>
                                            <SelectItem value="home" className="text-xs font-bold">{t('settings.sns_bots_tab.session_mode_home')}</SelectItem>
                                        </SelectContent>
                                    </Select>
                                </div>
                            </div>
                        </div>
                    ))}

                    {list.length === 0 && (
                        <div className="rounded-2xl border border-dashed border-border p-6 text-center text-xs italic text-muted-foreground bg-muted/5">
                            {t('settings.sns_bots_tab.empty_state')}
                        </div>
                    )}
                </div>
            </div>
        );
    };

    return (
        <div className="space-y-8 animate-in fade-in slide-in-from-bottom-2 duration-300">
            <div className="flex items-center justify-between">
                <div className="space-y-1">
                    <h4 className="text-sm font-bold flex items-center gap-2 text-foreground">
                        <MessageSquare className="w-5 h-5 text-primary" /> {t('settings.sns_bots_tab.heading')}
                    </h4>
                    <p className="text-xs text-muted-foreground">{t('settings.sns_bots_tab.description')}</p>
                </div>
                <Button
                    onClick={handleSave}
                    disabled={disabled}
                    className="border-primary/20 bg-primary/10 text-primary hover:bg-primary/20 rounded-xl text-xs font-bold"
                >
                    {isSaving ? <span className="animate-spin mr-2">⏳</span> : <Database className="w-4 h-4 mr-2" />}
                    {t('settings.sns_bots_tab.save_button')}
                </Button>
            </div>

            {loadState !== 'ready' && (
                <div role="status" className="space-y-2 text-sm">
                    <p>{t(loadState === 'loading' ? 'settings.sns_bots_tab.loading' : 'settings.sns_bots_tab.reload_required')}</p>
                    <Button variant="outline" disabled={isSaving || loadState === 'loading'} onClick={() => void fetchSnsBots()}>
                        {t('settings.sns_bots_tab.reload')}
                    </Button>
                </div>
            )}
            {tokenDraftCleared && <p role="status" className="text-sm">{t('settings.sns_bots_tab.token_draft_cleared')}</p>}
            <fieldset disabled={disabled} className="space-y-10">
                {renderBotList("discord")}
                <div className="border-t border-border/60" />
                {renderBotList("telegram")}
            </fieldset>
        </div>
    );
}
