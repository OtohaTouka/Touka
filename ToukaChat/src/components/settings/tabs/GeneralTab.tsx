import React, { useState, useRef } from "react";
import { cn } from "@/lib/utils";
import { Home, Upload, FileJson, Globe, Info, User as UserIcon, AlertTriangle, Download } from "lucide-react";
import { SUPPORTED_LNGS, LOCALE_ENDONYMS } from "@/locales";
import { APP_NAME, APP_VERSION } from "@/lib/app_meta";
import { useTranslation } from "react-i18next";
import { useSessionList } from "@/lib/providers/SessionListProvider";
import { useModels } from "@/lib/providers/ModelProvider";
import { formatToOpenWebUI, triggerDownload } from "@/lib/export-utils";
import { Session, User } from "@/lib/types";
import { getSystemNow, formatToTaipeiTime } from "@/lib/time_utils";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { toast } from "sonner";
import { errorMsg } from "@/lib/error_handler";
import { avatarSrc } from "@/lib/connections";
import { useAuth } from "@/contexts/AuthContext";
import { useUserSettings } from "@/hooks/settings/useUserSettings";
import { ChangePassword } from "@/components/settings/tabs/ChangePassword";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { useDirtyGuard } from "@/components/settings/SettingsDirtyContext";
import { McpOwnerDelegationPanel } from "@/components/settings/tabs/mcp/McpOwnerDelegationPanel";
import { useAgents } from "@/lib/providers/AgentProvider";

export function GeneralTab() {
    const { t, i18n } = useTranslation();
    const { sessions } = useSessionList();
    const { selectedModel } = useModels();
    const { user, refreshUser } = useAuth();
    const { selectedAgentId } = useAgents();
    const { updateOwnProfile, uploadOwnAvatar, deleteOwnAvatar, clearMyData, exportMyData, isSavingUser } = useUserSettings();

    const [selectedSessionIds, setSelectedSessionIds] = useState<Set<string>>(new Set());
    const fileInputRef = useRef<HTMLInputElement>(null);
    const avatarInputRef = useRef<HTMLInputElement>(null);

    // ADR-0106：頭像檔案上傳（前端預檢 type/size；後端為安全邊界）。成功後 refreshUser 同步新 avatar_url。
    const handleAvatarSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        e.target.value = ""; // 允許重選同檔
        if (!file) return;
        if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
            toast.error(t('settings.general_tab.avatar_invalid_format'));
            return;
        }
        if (file.size > 2 * 1024 * 1024) {
            toast.error(t('settings.general_tab.avatar_too_large'));
            return;
        }
        if (await uploadOwnAvatar(file)) await refreshUser();
    };

    const handleAvatarRemove = async () => {
        if (await deleteOwnAvatar()) await refreshUser();
    };

    // ADR-0107：自助清空個人資料（危險區，需密碼重輸的兩步確認）。
    const [showClearDialog, setShowClearDialog] = useState(false);
    const [clearPassword, setClearPassword] = useState("");

    const handleClearMyData = async () => {
        if (!clearPassword) return;
        const ok = await clearMyData(clearPassword);
        if (ok) {
            setShowClearDialog(false);
            setClearPassword("");
            await refreshUser();
        }
    };

    // 軌2 ADR-0110：自助 GDPR 資料匯出（加密 ZIP，需密碼 + 密語 ≥12 的兩步確認）。
    const [showExportDialog, setShowExportDialog] = useState(false);
    const [exportPassword, setExportPassword] = useState("");
    const [exportPassphrase, setExportPassphrase] = useState("");
    const exportReady = exportPassword.length > 0 && exportPassphrase.length >= 12;

    const handleExportMyData = async () => {
        if (!exportReady) return;
        const ok = await exportMyData(exportPassword, exportPassphrase);
        if (ok) {
            setShowExportDialog(false);
            setExportPassword("");
            setExportPassphrase("");
        }
    };

    // Profile State
    const [profileData, setProfileData] = useState<Partial<User>>({
        display_name: user?.display_name || "",
        avatar_url: user?.avatar_url || "",
        bio: user?.bio || ""
    });

    // user 變更時同步本地 profile 表單：採 React 官方「render 期間調整 state」模式（取代 set-state-in-effect）。
    // 比對 user 物件參考，與原 useEffect 的 [user] 相依等價。
    const [syncedUser, setSyncedUser] = useState<User | null>(user);
    if (user !== syncedUser) {
        setSyncedUser(user);
        if (user) {
            setProfileData({
                display_name: user.display_name || "",
                avatar_url: user.avatar_url || "",
                bio: user.bio || ""
            });
        }
    }

    // C5：個資表單未存草稿守衛（草稿與目前 user 不同即視為 dirty）。
    const profileDirty = !!user && (
        (profileData.display_name ?? "") !== (user.display_name ?? "") ||
        (profileData.avatar_url ?? "") !== (user.avatar_url ?? "") ||
        (profileData.bio ?? "") !== (user.bio ?? "")
    );
    useDirtyGuard("general-profile", profileDirty);

    const handleUpdateProfile = async () => {
        if (!user) return;
        // 自助更新自己的個資（身分由後端 JWT 決定，不傳 user.id）——一般使用者亦可，不再 403。
        const success = await updateOwnProfile(profileData);
        if (success) await refreshUser();
    };

    const toggleSessionSelection = (id: string) => {
        setSelectedSessionIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    const toggleSelectAll = () => {
        if (selectedSessionIds.size === sessions.length) {
            setSelectedSessionIds(new Set());
        } else {
            setSelectedSessionIds(new Set(sessions.map((s: Session) => s.id)));
        }
    };

    const handleSelectiveExport = () => {
        if (selectedSessionIds.size === 0) {
            toast.warning(t('settings.general_tab.toast_select_at_least_one'));
            return;
        }

        const selectedSessions = sessions.filter((s: Session) => selectedSessionIds.has(s.id));
        const exportData = selectedSessions.map((s: Session) => formatToOpenWebUI(s, selectedModel));
        
        triggerDownload(
            exportData, 
            `touka-backup-${getSystemNow().toISOString().slice(0, 10)}.json`
        );
        toast.success(t('settings.general_tab.toast_backup_success', { count: selectedSessionIds.size }));
    };

    const handleImport = (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (event) => {
            try {
                const importedData = JSON.parse(event.target?.result as string);
                // 成功提示由 useSessionEvents 在「確認並真的合併完成」後發出，避免取消／空匯入時誤報成功。
                window.dispatchEvent(new CustomEvent("touka_import_chats", { detail: importedData }));
            } catch {
                toast.error(errorMsg("VALIDATION_ERROR"));
            }
        };
        reader.readAsText(file);
    };

    return (
        <div className="space-y-10 animate-in fade-in slide-in-from-bottom-2 duration-300">
            {/* --- Profile Section --- */}
            <section className="space-y-6">
                <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                    <UserIcon className="w-4 h-4 text-primary" /> {t('settings.general_tab.profile_heading')}
                </h4>
                <div className="bg-muted/20 border border-border rounded-3xl p-6 space-y-6">
                    <div className="flex flex-col md:flex-row gap-8">
                        <div className="flex flex-col items-center gap-4 shrink-0">
                            <div className="w-24 h-24 rounded-3xl bg-primary/10 border border-primary/20 flex items-center justify-center overflow-hidden shadow-inner group relative">
                                {profileData.avatar_url ? (
                                    <img src={avatarSrc(profileData.avatar_url)} alt="Avatar" className="w-full h-full object-cover" />
                                ) : (
                                    <UserIcon className="w-10 h-10 text-primary/40" />
                                )}
                            </div>
                            {/* ADR-0106：頭像檔案上傳 */}
                            <input
                                ref={avatarInputRef}
                                type="file"
                                accept="image/jpeg,image/png,image/webp"
                                className="hidden"
                                aria-label={t('settings.general_tab.avatar_upload_aria')}
                                onChange={handleAvatarSelect}
                            />
                            <div className="flex flex-col items-stretch gap-1.5 w-24">
                                <Button
                                    variant="outline"
                                    onClick={() => avatarInputRef.current?.click()}
                                    disabled={isSavingUser}
                                    className="h-8 rounded-lg text-xs font-bold gap-1.5"
                                >
                                    <Upload className="w-3 h-3" /> {t('settings.general_tab.avatar_upload_button')}
                                </Button>
                                {profileData.avatar_url && (
                                    <Button
                                        variant="ghost"
                                        onClick={handleAvatarRemove}
                                        disabled={isSavingUser}
                                        className="h-7 rounded-lg text-xs text-muted-foreground hover:text-destructive"
                                    >
                                        {t('settings.general_tab.avatar_remove_button')}
                                    </Button>
                                )}
                            </div>
                        </div>
                        <div className="flex-1 space-y-4">
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                <div className="space-y-1.5">
                                    <label className="text-xs font-black uppercase text-muted-foreground px-1 tracking-widest">{t('settings.general_tab.display_name_label')}</label>
                                    <input
                                        type="text"
                                        value={profileData.display_name ?? ""}
                                        onChange={e => setProfileData({...profileData, display_name: e.target.value})}
                                        placeholder={t('settings.general_tab.display_name_placeholder')}
                                        className="w-full bg-background border-input rounded-xl border py-2.5 px-4 text-sm font-bold focus:ring-2 focus:ring-primary/50 outline-none transition-all"
                                    />
                                </div>
                                <div className="space-y-1.5">
                                    <label className="text-xs font-black uppercase text-muted-foreground px-1 tracking-widest">{t('settings.general_tab.avatar_url_label')}</label>
                                    <input 
                                        type="text"
                                        value={profileData.avatar_url ?? ""}
                                        onChange={e => setProfileData({...profileData, avatar_url: e.target.value})}
                                        placeholder="https://..."
                                        className="w-full bg-background border-input rounded-xl border py-2.5 px-4 text-sm font-bold focus:ring-2 focus:ring-primary/50 outline-none transition-all font-mono"
                                    />
                                </div>
                            </div>
                            <div className="space-y-1.5">
                                <label className="text-xs font-black uppercase text-muted-foreground px-1 tracking-widest">{t('settings.general_tab.bio_label')}</label>
                                <textarea
                                    value={profileData.bio ?? ""}
                                    onChange={e => setProfileData({...profileData, bio: e.target.value})}
                                    placeholder={t('settings.general_tab.bio_placeholder')}
                                    className="w-full bg-background border-input rounded-xl border py-2.5 px-4 text-sm font-medium focus:ring-2 focus:ring-primary/50 outline-none transition-all h-20 resize-none"
                                />
                            </div>
                        </div>
                    </div>
                    <div className="flex justify-end pt-2">
                        <Button 
                            onClick={handleUpdateProfile}
                            disabled={isSavingUser}
                            className="px-8 h-10 rounded-xl font-black text-sm"
                        >
                            {t('settings.general_tab.save_profile_button')}
                        </Button>
                    </div>
                </div>
            </section>

            <McpOwnerDelegationPanel agentId={selectedAgentId} />

            {/* P2: 自助修改密碼（帳號區，全使用者可用） */}
            <ChangePassword />

            {/* ADR-0107: 危險區 — 自助清空個人資料 */}
            <section className="space-y-3">
                <div className="rounded-2xl border border-destructive/40 bg-destructive/5 p-5 space-y-3">
                    <div className="flex items-start gap-3">
                        <AlertTriangle className="w-5 h-5 text-destructive shrink-0 mt-0.5" />
                        <div className="space-y-1">
                            <h4 className="text-sm font-black text-destructive">{t('settings.general_tab.clear_data_title')}</h4>
                            <p className="text-xs text-muted-foreground leading-relaxed">{t('settings.general_tab.clear_data_hint')}</p>
                        </div>
                    </div>
                    <div className="flex justify-end">
                        <Button
                            variant="destructive"
                            onClick={() => setShowClearDialog(true)}
                            disabled={isSavingUser}
                            className="h-9 rounded-xl text-xs font-bold"
                        >
                            {t('settings.general_tab.clear_data_button')}
                        </Button>
                    </div>
                </div>
            </section>

            <Dialog open={showClearDialog} onOpenChange={(o) => { if (!o) { setShowClearDialog(false); setClearPassword(""); } }}>
                <DialogContent
                    overlayClassName="bg-background/60 backdrop-blur-md"
                    className="bg-card border border-destructive/40 rounded-3xl p-8 max-w-md w-full shadow-2xl space-y-5"
                >
                    <div className="flex items-center gap-3 text-destructive">
                        <AlertTriangle className="w-6 h-6 shrink-0" />
                        <DialogTitle className="text-lg font-black">{t('settings.general_tab.clear_data_confirm_title')}</DialogTitle>
                    </div>
                    <DialogDescription className="text-sm text-foreground/80 leading-relaxed">
                        {t('settings.general_tab.clear_data_confirm_body')}
                    </DialogDescription>
                    <div className="space-y-1.5">
                        <label htmlFor="clear-data-password" className="text-xs font-black uppercase text-muted-foreground tracking-widest">{t('settings.general_tab.clear_data_password_label')}</label>
                        <input
                            id="clear-data-password"
                            type="password"
                            value={clearPassword}
                            onChange={(e) => setClearPassword(e.target.value)}
                            autoComplete="current-password"
                            className="w-full bg-background border border-border rounded-xl py-2.5 px-4 text-sm focus:ring-2 focus:ring-destructive/50 outline-none"
                        />
                    </div>
                    <div className="grid grid-cols-2 gap-3 pt-1">
                        <Button variant="outline" onClick={() => { setShowClearDialog(false); setClearPassword(""); }} className="h-10 rounded-xl text-xs font-bold">
                            {t('settings.general_tab.clear_data_cancel')}
                        </Button>
                        <Button variant="destructive" onClick={handleClearMyData} disabled={isSavingUser || !clearPassword} className="h-10 rounded-xl text-xs font-bold">
                            {t('settings.general_tab.clear_data_confirm_action')}
                        </Button>
                    </div>
                </DialogContent>
            </Dialog>

            {/* 軌2 ADR-0110: 資料匯出（GDPR 資料可攜權，加密 ZIP） */}
            <section className="space-y-3">
                <div className="rounded-2xl border border-border bg-muted/20 p-5 space-y-3">
                    <div className="flex items-start gap-3">
                        <Download className="w-5 h-5 text-primary shrink-0 mt-0.5" />
                        <div className="space-y-1">
                            <h4 className="text-sm font-black text-foreground">{t('settings.general_tab.export_data_title')}</h4>
                            <p className="text-xs text-muted-foreground leading-relaxed">{t('settings.general_tab.export_data_hint')}</p>
                        </div>
                    </div>
                    <div className="flex justify-end">
                        <Button
                            variant="outline"
                            onClick={() => setShowExportDialog(true)}
                            disabled={isSavingUser}
                            className="h-9 rounded-xl text-xs font-bold gap-1.5"
                        >
                            <Download className="w-3.5 h-3.5" /> {t('settings.general_tab.export_data_button')}
                        </Button>
                    </div>
                </div>
            </section>

            <Dialog open={showExportDialog} onOpenChange={(o) => { if (!o) { setShowExportDialog(false); setExportPassword(""); setExportPassphrase(""); } }}>
                <DialogContent
                    overlayClassName="bg-background/60 backdrop-blur-md"
                    className="bg-card border border-border rounded-3xl p-8 max-w-md w-full shadow-2xl space-y-5"
                >
                    <div className="flex items-center gap-3 text-primary">
                        <Download className="w-6 h-6 shrink-0" />
                        <DialogTitle className="text-lg font-black text-foreground">{t('settings.general_tab.export_data_confirm_title')}</DialogTitle>
                    </div>
                    <DialogDescription className="text-sm text-foreground/80 leading-relaxed">
                        {t('settings.general_tab.export_data_confirm_body')}
                    </DialogDescription>
                    <div className="space-y-1.5">
                        <label htmlFor="export-data-password" className="text-xs font-black uppercase text-muted-foreground tracking-widest">{t('settings.general_tab.export_data_password_label')}</label>
                        <input
                            id="export-data-password"
                            type="password"
                            value={exportPassword}
                            onChange={(e) => setExportPassword(e.target.value)}
                            autoComplete="current-password"
                            className="w-full bg-background border border-border rounded-xl py-2.5 px-4 text-sm focus:ring-2 focus:ring-primary/50 outline-none"
                        />
                    </div>
                    <div className="space-y-1.5">
                        <label htmlFor="export-data-passphrase" className="text-xs font-black uppercase text-muted-foreground tracking-widest">{t('settings.general_tab.export_data_passphrase_label')}</label>
                        <input
                            id="export-data-passphrase"
                            type="password"
                            value={exportPassphrase}
                            onChange={(e) => setExportPassphrase(e.target.value)}
                            autoComplete="new-password"
                            className="w-full bg-background border border-border rounded-xl py-2.5 px-4 text-sm font-mono focus:ring-2 focus:ring-primary/50 outline-none"
                        />
                        <p className="text-xs text-muted-foreground px-1">{t('settings.general_tab.export_data_passphrase_hint')}</p>
                    </div>
                    <div className="grid grid-cols-2 gap-3 pt-1">
                        <Button variant="outline" onClick={() => { setShowExportDialog(false); setExportPassword(""); setExportPassphrase(""); }} className="h-10 rounded-xl text-xs font-bold">
                            {t('settings.general_tab.export_data_cancel')}
                        </Button>
                        <Button onClick={handleExportMyData} disabled={isSavingUser || !exportReady} className="h-10 rounded-xl text-xs font-bold">
                            {t('settings.general_tab.export_data_confirm_action')}
                        </Button>
                    </div>
                </DialogContent>
            </Dialog>

            <section className="space-y-3">
                <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                    <Globe className="w-4 h-4 text-primary" /> {t('settings.language')}
                </h4>
                <div className="grid grid-cols-2 gap-3 max-w-sm">
                    {SUPPORTED_LNGS.map(lng => (
                        <Button
                            key={lng}
                            variant={i18n.resolvedLanguage === lng ? "default" : "outline"}
                            onClick={() => i18n.changeLanguage(lng)}
                            className={cn("h-11 justify-start px-4", i18n.resolvedLanguage === lng && "scale-105 shadow-lg")}
                        >
                            {LOCALE_ENDONYMS[lng] ?? lng}
                        </Button>
                    ))}
                </div>
            </section>

            <section className="space-y-4 pt-4">
                <h4 className="text-sm font-bold text-foreground flex items-center justify-between">
                    <span>{t('settings.general_tab.backup_migration_heading')}</span>
                    <button
                        onClick={toggleSelectAll}
                        className="text-xs font-black uppercase tracking-widest text-primary transition-all hover:opacity-80"
                    >
                        {selectedSessionIds.size === sessions.length ? t('settings.general_tab.deselect_all') : t('settings.general_tab.select_all_sessions')}
                    </button>
                </h4>
                
                <div className="grid grid-cols-1 gap-4">
                    {/* Selective Export List */}
                    <div className="rounded-2xl border border-border bg-muted/20 p-4 space-y-4">
                        <div className="custom-scrollbar max-h-56 space-y-2 overflow-y-auto pr-2">
                            {sessions.length > 0 ? sessions.map((s: Session) => (
                                <label 
                                    key={s.id} 
                                    className={cn(
                                        "flex items-center gap-3 p-3 rounded-xl border transition-all cursor-pointer group",
                                        selectedSessionIds.has(s.id) 
                                            ? "border-primary/40 bg-primary/5 shadow-sm" 
                                            : "border-transparent bg-background/50 hover:border-border hover:bg-accent/50"
                                    )}
                                >
                                    <Checkbox 
                                        checked={selectedSessionIds.has(s.id)} 
                                        onCheckedChange={() => toggleSessionSelection(s.id)}
                                    />
                                    <div className="flex-1 min-w-0">
                                        <div className="truncate text-sm font-bold text-foreground">{s.title || t('settings.general_tab.untitled_session')}</div>
                                        <div className="mt-0.5 text-xs text-muted-foreground">{formatToTaipeiTime(s.updatedAt)}</div>
                                    </div>
                                    {s.is_home && <Home size={12} className="text-primary opacity-60" />}
                                </label>
                            )) : (
                                <div className="text-center py-6 text-muted-foreground text-xs italic">{t('settings.general_tab.no_sessions')}</div>
                            )}
                        </div>

                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2">
                            <Button 
                                variant="outline"
                                onClick={() => fileInputRef.current?.click()} 
                                className="h-10 border-primary/20 bg-primary/5 text-primary hover:bg-primary/10"
                            >
                                <Upload className="w-4 h-4 mr-2" /> {t('settings.general_tab.import_button')}
                            </Button>
                            <Button 
                                onClick={handleSelectiveExport} 
                                disabled={selectedSessionIds.size === 0}
                                className={cn(
                                    "h-10",
                                    selectedSessionIds.size > 0 && "scale-105 shadow-lg shadow-primary/20"
                                )}
                            >
                                <FileJson className="w-4 h-4 mr-2" /> {t('settings.general_tab.export_selected_button', { count: selectedSessionIds.size })}
                            </Button>
                        </div>
                        <input type="file" accept=".json" className="hidden" ref={fileInputRef} onChange={handleImport} />
                    </div>
                </div>
            </section>

            {/* --- About Section（HAX G18：版本顯示，SSOT＝package.json）--- */}
            <section className="space-y-3 pt-4">
                <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                    <Info className="w-4 h-4 text-primary" /> {t('settings.general_tab.about_heading')}
                </h4>
                <div className="rounded-2xl border border-border bg-muted/20 p-4 space-y-1">
                    <div className="flex items-center gap-3">
                        <span className="text-sm font-bold text-foreground">{APP_NAME}</span>
                        <span className="rounded-md bg-muted px-2 py-0.5 text-xs font-mono text-muted-foreground">v{APP_VERSION}</span>
                    </div>
                    <p className="text-xs text-muted-foreground">{t('settings.general_tab.about_description')}</p>
                </div>
            </section>
        </div>
    );
}
