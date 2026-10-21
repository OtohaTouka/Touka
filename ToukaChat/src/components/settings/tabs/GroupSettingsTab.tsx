import { useEffect, useState } from 'react';
import { useGroupSettings, GroupWhitelist } from '@/hooks/settings/useGroupSettings';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Users, Trash2, Save, Loader2, UserCheck, Hash } from 'lucide-react';
import { toast } from 'sonner';
import { errorMsg } from '@/lib/error_handler';
import { useTranslation } from 'react-i18next';
import { useDirtyGuard } from '@/components/settings/SettingsDirtyContext';

export function GroupSettingsTab() {
    const { t } = useTranslation();
    const { whitelist, availableModels, isLoading, fetchWhitelist, fetchModels, saveWhitelist } = useGroupSettings();
    const [localWhitelist, setLocalWhitelist] = useState<GroupWhitelist>(whitelist);
    const [isSaving, setIsSaving] = useState(false);
    // P1：別名編輯草稿，以穩定的靈魂 ID 為鍵。controlled input 寫這裡，
    // 存檔時才 reconcile 成新的別名 key——避免逐字元改 key 造成中間 key／跳焦點。
    const [aliasDrafts, setAliasDrafts] = useState<Record<string, string>>({});
    const [dirty, setDirty] = useState(false);
    useDirtyGuard("group-settings", dirty);  // C5：未存草稿守衛

    useEffect(() => {
        fetchWhitelist();
        fetchModels();
    }, [fetchWhitelist, fetchModels]);

    useEffect(() => {
        if (whitelist) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- whitelist 載入後同步本地可編輯副本＋乾淨基線
            setLocalWhitelist(whitelist);
            setDirty(false);
        }
    }, [whitelist]);

    // 把別名草稿套用為最終別名 key（空白草稿回退既有別名）；「所見即所存」，無需 blur。
    const reconcileDrafts = (wl: GroupWhitelist): GroupWhitelist => {
        const next: Record<string, string> = {};
        Object.entries(wl.ToukaSoul || {}).forEach(([alias, id]) => {
            const draft = aliasDrafts[id];
            next[draft && draft.trim() ? draft.trim() : alias] = id;
        });
        return { ToukaSoul: next };
    };

    const handleSave = async () => {
        setIsSaving(true);
        const payload = reconcileDrafts(localWhitelist);
        const success = await saveWhitelist(payload);
        if (success) {
            setLocalWhitelist(payload);
            setAliasDrafts({});
            setDirty(false);
            toast.success(t('settings.group_settings_tab.whitelist_updated'));
        } else {
            toast.error(errorMsg("CONFIG_SAVE_FAILED"));
        }
        setIsSaving(false);
    };

    const addItem = (soulId: string) => {
        // 預設別名為 ID 本身 (SSOT-aligned)
        setLocalWhitelist(prev => ({ ToukaSoul: { ...prev.ToukaSoul, [soulId]: soulId } }));
        setDirty(true);
    };

    const removeItem = (alias: string) => {
        const next = { ...localWhitelist.ToukaSoul };
        delete next[alias];
        setLocalWhitelist({ ToukaSoul: next });
        setDirty(true);
    };

    // 群聊成員只限靈魂（ADR-0245）：只有靈魂一區
    const soulSection = (
        <div className="space-y-4 rounded-3xl border border-border bg-muted/20 p-6">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <div className="rounded-xl bg-primary/10 p-2 text-primary"><UserCheck size={18} /></div>
                    <div>
                        <h3 className="text-sm font-bold">{t('settings.group_settings_tab.section_soul_title')}</h3>
                        <p className="text-xs text-muted-foreground">{t('settings.group_settings_tab.section_soul_desc')}</p>
                    </div>
                </div>
            </div>

            <div className="space-y-2">
                {Object.entries(localWhitelist.ToukaSoul || {}).map(([alias, id]) => (
                    <div key={id} className="flex items-center gap-3 p-3 bg-background rounded-2xl border border-border group">
                        <div className="flex-1 flex items-center gap-2">
                            <Hash size={14} className="text-muted-foreground" />
                            <Input
                                className="h-8 text-xs font-bold bg-transparent border-none focus-visible:ring-1 focus-visible:ring-primary/50"
                                aria-label={t('settings.group_settings_tab.alias_input_aria', { id })}
                                value={aliasDrafts[id] ?? alias}
                                onChange={(e) => { setAliasDrafts(d => ({ ...d, [id]: e.target.value })); setDirty(true); }}
                                placeholder={t('settings.group_settings_tab.alias_placeholder')}
                            />
                        </div>
                        <div className="flex min-w-32 flex-col items-end border-l border-border/50 px-3">
                            <span className="text-xs uppercase text-muted-foreground">{t('settings.group_settings_tab.points_to_entity')}</span>
                            <span className="max-w-40 truncate font-mono text-xs font-bold">{id}</span>
                        </div>
                        <Button
                            variant="ghost"
                            size="icon"
                            aria-label={t('settings.group_settings_tab.remove_alias_aria', { alias })}
                            className="h-8 w-8 text-destructive transition-opacity"
                            onClick={() => removeItem(alias)}
                        >
                            <Trash2 size={14} />
                        </Button>
                    </div>
                ))}
            </div>

            <div className="flex gap-2 pt-2">
                <select
                    aria-label={t('settings.group_settings_tab.add_model_aria')}
                    className="flex-1 bg-background border border-input rounded-xl px-3 py-2 text-xs focus:ring-2 focus:ring-primary outline-none"
                    onChange={(e) => {
                        if (e.target.value) addItem(e.target.value);
                        e.target.value = "";
                    }}
                >
                    <option value="">{t('settings.group_settings_tab.add_model_option')}</option>
                    {availableModels
                        // 只列靈魂，且排除已被任何別名指向的（防重複）
                        .filter(m => m.owned_by === "touka" && m.id !== "ToukaGroup"
                            && !Object.values(localWhitelist.ToukaSoul || {}).includes(m.id))
                        .map(m => (
                            <option key={m.id} value={m.id}>{m.id} ({m.owned_by})</option>
                        ))
                    }
                </select>
            </div>
        </div>
    );

    return (
        <div className="space-y-6 animate-in fade-in slide-in-from-bottom-2 duration-500">
            <div className="flex items-center justify-between px-2">
                <div>
                    <h2 className="text-xl font-black tracking-tight flex items-center gap-2">
                        <Users className="text-primary" /> {t('settings.group_settings_tab.title')}
                    </h2>
                    <p className="text-xs text-muted-foreground">{t('settings.group_settings_tab.subtitle')}</p>
                </div>
                <Button 
                    onClick={handleSave} 
                    disabled={isSaving || isLoading}
                    className="rounded-2xl gap-2 px-6"
                >
                    {isSaving ? <Loader2 className="animate-spin" size={16} /> : <Save size={16} />}
                    {t('settings.group_settings_tab.save_button')}
                </Button>
            </div>

            {isLoading ? (
                <div
                    className="flex justify-center py-16"
                    role="status"
                    aria-busy="true"
                    aria-label={t('settings.group_settings_tab.loading')}
                >
                    <Loader2 className="animate-spin text-primary" size={32} />
                </div>
            ) : (
                <div className="grid grid-cols-1 gap-6">{soulSection}</div>
            )}
        </div>
    );
}
