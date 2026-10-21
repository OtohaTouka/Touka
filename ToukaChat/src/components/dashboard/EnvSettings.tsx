"use client";

import { useState, useEffect, useCallback } from "react";
import {
    Save, RefreshCw, AlertTriangle,
    CheckCircle, Database, History,
    Clock, FileArchive, Zap, Trash2, HardDrive, Key, Download, Lock
} from "lucide-react";
import { toast } from "sonner";
import { useTranslation, Trans } from "react-i18next";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { cn } from "@/lib/utils";
import { logSSOTError, errorMsg, getErrorMessage } from "@/lib/error_handler";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { useRestoreWatch } from "@/hooks/useRestoreWatch";
import {
    Dialog,
    DialogContent,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";
import { useDirtyGuard } from "@/components/settings/SettingsDirtyContext";
import { SettingsSnapshotCard } from "@/components/dashboard/SettingsSnapshotCard";

interface BackupInfo {
    filename: string;
    size_mb: number;
    created_at: string;
}

interface BackupStats {
    count: number;
    total_size_mb: number;
    oldest: string | null;
    newest: string | null;
    free_disk_mb: number;
}

interface BackupSchedule {
    enabled: boolean;
    frequency: "daily" | "weekly";
    time_of_day: string;
    weekday: number;
    retention_count: number;
    last_run_epoch: number | null;
}

export function EnvSettings() {
    const { t } = useTranslation();
    const { confirmToast } = useConfirmToast();
    const [backups, setBackups] = useState<BackupInfo[]>([]);
    const [stats, setStats] = useState<BackupStats | null>(null);
    const [isLoading, setIsLoading] = useState(true);
    const [isBackingUp, setIsBackingUp] = useState(false);
    const [isRestoring, setIsRestoring] = useState<string | null>(null);
    const [message, setMessage] = useState("");
    const [showConfirm, setShowConfirm] = useState<string | null>(null);
    // P1a：還原為破壞性操作 → 需重輸密碼確認（後端 verify_password 為真守衛，前端防誤觸）。
    const [restorePassword, setRestorePassword] = useState("");
    // P2（ADR-0108）：加密下載——下載前設密碼短語，後端即時加密成 WinZip-AES-256 zip。
    const [downloadTarget, setDownloadTarget] = useState<string | null>(null);
    const [downloadPassphrase, setDownloadPassphrase] = useState("");
    // P3（ADR-0109）：自動備份排程。
    const [schedule, setSchedule] = useState<BackupSchedule | null>(null);
    const [isSavingSchedule, setIsSavingSchedule] = useState(false);
    // C5：以最後一次載入/儲存的排程為基線，比對出未存草稿。
    const [savedScheduleJson, setSavedScheduleJson] = useState<string | null>(null);

    const fetchSchedule = async () => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/system/backup/schedule");
            if (res.ok) {
                const data = await res.json();
                setSchedule(data);
                setSavedScheduleJson(JSON.stringify(data));
            }
        } catch (e) {
            logSSOTError(e, "EnvSettings:fetchSchedule");
        }
    };

    const handleSaveSchedule = async () => {
        if (!schedule) return;
        setIsSavingSchedule(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/system/backup/schedule", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    enabled: schedule.enabled,
                    frequency: schedule.frequency,
                    time_of_day: schedule.time_of_day,
                    weekday: schedule.weekday,
                    retention_count: schedule.retention_count,
                }),
            });
            if (res.ok) {
                const saved = await res.json();
                setSchedule(saved);
                setSavedScheduleJson(JSON.stringify(saved));
                toast.success(t("dashboard.env_settings.schedule_saved"));
            } else {
                toast.error(await getErrorMessage(res, "SYSTEM_ERROR"));
            }
        } catch (e) {
            logSSOTError(e, "EnvSettings:handleSaveSchedule");
            toast.error(errorMsg("SYSTEM_ERROR"));
        } finally {
            setIsSavingSchedule(false);
        }
    };

    const fetchStats = async () => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/system/backups/stats");
            if (res.ok) {
                const d = await res.json();
                // 僅接受物件形狀（防後端/mock 回陣列時渲染出 undefined）。
                if (d && typeof d === "object" && !Array.isArray(d)) setStats(d as BackupStats);
            }
        } catch (e) {
            logSSOTError(e, "EnvSettings:fetchStats");
        }
    };

    const fetchBackups = async () => {
        setIsLoading(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/system/backups");
            if (res.ok) {
                const data = await res.json();
                setBackups(data);
            }
        } catch (e) {
            logSSOTError(e, "EnvSettings:fetchBackups");
        } finally {
            setIsLoading(false);
        }
        await fetchStats();
    };

    const handleDelete = (filename: string) => {
        confirmToast(
            t("dashboard.env_settings.delete_confirm_message", { filename }),
            () => doDelete(filename),
            t("dashboard.env_settings.delete_confirm_action"),
        );
    };

    const doDelete = async (filename: string) => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, `/v1/system/backups/${filename}`, {
                method: "DELETE",
            });
            if (res.ok) {
                toast.success(t("dashboard.env_settings.delete_success"));
                await fetchBackups();
            } else {
                toast.error(await getErrorMessage(res, "SYSTEM_ERROR"));
            }
        } catch (e) {
            logSSOTError(e, "EnvSettings:doDelete");
            toast.error(errorMsg("SYSTEM_ERROR"));
        }
    };

    const handleDownload = async (filename: string, passphrase: string) => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, `/v1/system/backups/${filename}/download`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ passphrase }),
            });
            if (res.ok) {
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `${filename.replace(/\.zip$/, "")}_encrypted.zip`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                toast.success(t("dashboard.env_settings.download_success"));
            } else {
                toast.error(await getErrorMessage(res, "SYSTEM_ERROR"));
            }
        } catch (e) {
            logSSOTError(e, "EnvSettings:handleDownload");
            toast.error(errorMsg("SYSTEM_ERROR"));
        }
    };

    const handleCreateBackup = async () => {
        setIsBackingUp(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/system/backup/run", { method: "POST" });
            if (res.ok) {
                setMessage(t("dashboard.env_settings.backup_success"));
                await fetchBackups();
                setTimeout(() => setMessage(""), 5000);
            } else {
                // F2：非 ok（403/500…）不再靜默——回饋具體錯誤。
                toast.error(await getErrorMessage(res, "SYSTEM_BACKUP_FAILED"));
            }
        } catch (e) {
            logSSOTError(e, "EnvSettings:handleCreateBackup");
            toast.error(errorMsg("SYSTEM_BACKUP_FAILED"));
        } finally {
            setIsBackingUp(false);
        }
    };

    const handleRestore = async (filename: string, currentPassword: string) => {
        setIsRestoring(filename);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/system/restore/run", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ filename, current_password: currentPassword })
            });
            if (res.ok) {
                // Restore logic triggered. Server will die soon.
                // We show a countdown or static "Restoring" screen.
            } else {
                // F3：依後端錯誤碼分流（404 檔案不存在 / 403 權限 / 500…），不再一律誤報權限。
                setIsRestoring(null);
                toast.error(await getErrorMessage(res, "SYSTEM_ERROR"));
            }
        } catch (e) {
            logSSOTError(e, "EnvSettings:handleRestore");
            // This is expected as the server might shut down
        }
    };

    useEffect(() => {
        fetchBackups();
        fetchSchedule();
    }, []);

    // B1／H1：還原中先等、再查狀態才重整（計時器在 useRestoreWatch 的 effect 內、卸載清除）。
    // 失敗就跳常駐錯誤、回到備份清單，不重整；成功（伺服器重啟）或查不到照舊重整。
    const handleRestoreFailed = useCallback(() => {
        setIsRestoring(null);
        toast.error(t("dashboard.env_settings.restore_failed"), { duration: Infinity, closeButton: true });
    }, [t]);
    useRestoreWatch(isRestoring !== null, handleRestoreFailed);

    // C5：排程表單未存草稿守衛——schedule 與基線不同即 dirty（切分頁/關閉前警告）。
    const scheduleDirty =
        schedule !== null && savedScheduleJson !== null
        && JSON.stringify(schedule) !== savedScheduleJson;
    useDirtyGuard("env-backup-schedule", scheduleDirty);

    if (isRestoring) {
        // A3：還原中全螢幕狀態改 Radix Dialog（aria-modal/焦點陷阱/role），刻意不可關閉
        //（等待服務重啟），故攔截 Esc 與外點。
        return (
            <Dialog open>
                <DialogContent
                    overlayClassName="bg-background/90 backdrop-blur-xl"
                    onEscapeKeyDown={(e) => e.preventDefault()}
                    onPointerDownOutside={(e) => e.preventDefault()}
                    onInteractOutside={(e) => e.preventDefault()}
                    className="flex flex-col items-center justify-center space-y-8 border-0 bg-transparent shadow-none"
                >
                    <div className="relative">
                        <div className="w-24 h-24 border-4 border-primary/20 border-t-primary rounded-full animate-spin" />
                        <Database className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-8 h-8 text-primary animate-pulse" />
                    </div>
                    <div className="text-center space-y-2">
                        <DialogTitle className="text-2xl font-black uppercase tracking-tighter italic">{t("dashboard.env_settings.restoring_title")}</DialogTitle>
                        <DialogDescription className="text-muted-foreground font-medium">
                            {t("dashboard.env_settings.restoring_applying_backup")}<span className="text-primary">{isRestoring}</span>
                            {/* A3：勿關閉警告併入 description，確保 aria-describedby 涵蓋（讀屏會宣讀） */}
                            <span className="mt-4 block text-xs font-bold uppercase tracking-widest text-status-warning">{t("dashboard.env_settings.restoring_wait_notice")}</span>
                        </DialogDescription>
                    </div>
                </DialogContent>
            </Dialog>
        );
    }

    return (
        <div className="space-y-8 animate-in fade-in duration-500">
            {/* Header Area */}
            <div className="flex items-center justify-between">
                <div className="space-y-1">
                    <h3 className="text-lg font-black text-foreground uppercase tracking-tight italic flex items-center gap-2">
                        <History className="w-5 h-5 text-primary" /> {t("dashboard.env_settings.header_title")}
                    </h3>
                    <p className="text-xs font-bold text-muted-foreground uppercase tracking-widest ml-7">{t("dashboard.env_settings.header_subtitle")}</p>
                </div>
                
                <div className="flex items-center gap-4">
                    {message && (
                        <span className="text-xs font-bold text-status-success animate-pulse flex items-center gap-1">
                            <CheckCircle className="w-3 h-3" /> {message}
                        </span>
                    )}
                    <button 
                        onClick={handleCreateBackup}
                        disabled={isBackingUp || isLoading}
                        className={cn(
                            "flex items-center gap-2 px-6 py-2.5 rounded-xl text-xs font-black shadow-lg transition-all active:scale-95 disabled:opacity-50",
                            isBackingUp ? "bg-muted text-muted-foreground" : "bg-primary-button text-primary-foreground hover:scale-105"
                        )}
                    >
                        {isBackingUp ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Database className="w-4 h-4" />} 
                        {isBackingUp ? t("dashboard.env_settings.creating_snapshot") : t("dashboard.env_settings.create_backup_now")}
                    </button>
                    <button
                        onClick={fetchBackups}
                        aria-label={t("dashboard.env_settings.refresh_backups_aria")}
                        className="p-2.5 rounded-xl border border-border hover:bg-accent transition-colors"
                    >
                        <RefreshCw className={cn("w-4 h-4", isLoading && "animate-spin")} />
                    </button>
                </div>
            </div>

            {/* Backup List */}
            <div className="space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2 px-2">
                    <div className="flex items-center gap-2">
                        <div className="h-4 w-1 bg-primary rounded-full"></div>
                        <h4 className="text-xs font-black uppercase tracking-widest text-muted-foreground">{t("dashboard.env_settings.backup_history_title")}</h4>
                    </div>
                    {stats && (
                        <div className="flex flex-wrap items-center gap-3 text-xs font-bold text-muted-foreground">
                            <span className="flex items-center gap-1"><FileArchive size={12} /> {t("dashboard.env_settings.stats_backups", { count: stats.count })}</span>
                            <span className="flex items-center gap-1"><Save size={12} /> {t("dashboard.env_settings.stats_total", { size: stats.total_size_mb })}</span>
                            <span className="flex items-center gap-1"><HardDrive size={12} /> {t("dashboard.env_settings.stats_free", { free: stats.free_disk_mb })}</span>
                        </div>
                    )}
                </div>

                {isLoading && backups.length === 0 ? (
                    <div className="py-20 flex flex-col items-center justify-center gap-4 opacity-50">
                        <RefreshCw className="w-8 h-8 animate-spin text-primary" />
                        <span className="text-xs font-black uppercase tracking-widest">{t("dashboard.env_settings.loading_list")}</span>
                    </div>
                ) : backups.length === 0 ? (
                    <div className="py-20 border-2 border-dashed border-border rounded-3xl flex flex-col items-center justify-center gap-4 group">
                        <div className="w-16 h-16 rounded-full bg-muted/50 flex items-center justify-center group-hover:bg-primary/5 transition-colors">
                            <FileArchive className="w-8 h-8 text-muted-foreground group-hover:text-primary/40" />
                        </div>
                        <p className="text-xs font-bold text-muted-foreground uppercase tracking-widest italic">{t("dashboard.env_settings.no_backups")}</p>
                    </div>
                ) : (
                    <div className="grid grid-cols-1 gap-3">
                        {backups.map(backup => (
                            <div key={backup.filename} className="group p-4 bg-card hover:bg-accent/5 transition-all border border-border hover:border-primary/30 rounded-2xl flex items-center justify-between">
                                <div className="flex items-center gap-4">
                                    <div className="w-10 h-10 rounded-xl bg-primary/5 flex items-center justify-center group-hover:scale-110 transition-transform">
                                        <FileArchive className="w-5 h-5 text-primary" />
                                    </div>
                                    <div className="space-y-0.5">
                                        <div className="text-sm font-black text-foreground font-mono">{backup.filename}</div>
                                        <div className="flex items-center gap-3 text-xs text-muted-foreground font-bold uppercase tracking-wider">
                                            <span className="flex items-center gap-1"><Clock size={10} /> {backup.created_at}</span>
                                            <span className="flex items-center gap-1 opacity-60"><Save size={10} /> {backup.size_mb} MB</span>
                                        </div>
                                    </div>
                                </div>

                                <div className="flex items-center gap-2">
                                    <button
                                        onClick={() => setShowConfirm(backup.filename)}
                                        className="flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-black uppercase tracking-widest border border-primary/20 text-primary hover:bg-primary-button hover:text-white transition-all shadow-sm"
                                    >
                                        <Zap className="w-3 h-3" /> {t("dashboard.env_settings.restore_this_version")}
                                    </button>
                                    <button
                                        onClick={() => setDownloadTarget(backup.filename)}
                                        aria-label={t("dashboard.env_settings.download_backup_aria", { filename: backup.filename })}
                                        className="p-2 rounded-xl border border-border text-muted-foreground hover:border-primary hover:bg-primary/10 hover:text-primary transition-all"
                                    >
                                        <Download className="w-4 h-4" />
                                    </button>
                                    <button
                                        onClick={() => handleDelete(backup.filename)}
                                        aria-label={t("dashboard.env_settings.delete_backup_aria", { filename: backup.filename })}
                                        className="p-2 rounded-xl border border-border text-muted-foreground hover:border-status-error hover:bg-status-error/10 hover:text-status-error transition-all"
                                    >
                                        <Trash2 className="w-4 h-4" />
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </div>

            {/* Auto-backup schedule (ADR-0109) */}
            {schedule && (
                <div className="space-y-4 rounded-3xl border border-border bg-muted/20 p-6">
                    <div className="flex items-center gap-2">
                        <Clock className="w-4 h-4 text-primary" />
                        <h4 className="text-xs font-black uppercase tracking-widest text-muted-foreground">{t("dashboard.env_settings.schedule_title")}</h4>
                    </div>
                    <label className="flex items-center gap-3 text-sm font-bold text-foreground cursor-pointer">
                        <input
                            type="checkbox"
                            checked={schedule.enabled}
                            onChange={e => setSchedule({ ...schedule, enabled: e.target.checked })}
                            className="h-4 w-4 accent-primary"
                        />
                        {t("dashboard.env_settings.schedule_enable")}
                    </label>
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                        <div className="space-y-1.5">
                            <label htmlFor="sched-freq" className="text-xs font-bold text-muted-foreground">{t("dashboard.env_settings.schedule_frequency")}</label>
                            <select
                                id="sched-freq"
                                value={schedule.frequency}
                                onChange={e => setSchedule({ ...schedule, frequency: e.target.value as "daily" | "weekly" })}
                                className="w-full bg-background border border-input rounded-xl px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/50"
                            >
                                <option value="daily">{t("dashboard.env_settings.schedule_daily")}</option>
                                <option value="weekly">{t("dashboard.env_settings.schedule_weekly")}</option>
                            </select>
                        </div>
                        {schedule.frequency === "weekly" && (
                            <div className="space-y-1.5">
                                <label htmlFor="sched-wd" className="text-xs font-bold text-muted-foreground">{t("dashboard.env_settings.schedule_weekday")}</label>
                                <select
                                    id="sched-wd"
                                    value={schedule.weekday}
                                    onChange={e => setSchedule({ ...schedule, weekday: Number(e.target.value) })}
                                    className="w-full bg-background border border-input rounded-xl px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/50"
                                >
                                    {[0, 1, 2, 3, 4, 5, 6].map(d => (
                                        <option key={d} value={d}>{t(`dashboard.env_settings.schedule_wd_${d}`)}</option>
                                    ))}
                                </select>
                            </div>
                        )}
                        <div className="space-y-1.5">
                            <label htmlFor="sched-time" className="text-xs font-bold text-muted-foreground">{t("dashboard.env_settings.schedule_time")}</label>
                            <input
                                id="sched-time"
                                type="time"
                                value={schedule.time_of_day}
                                onChange={e => setSchedule({ ...schedule, time_of_day: e.target.value })}
                                className="w-full bg-background border border-input rounded-xl px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/50"
                            />
                        </div>
                        <div className="space-y-1.5">
                            <label htmlFor="sched-keep" className="text-xs font-bold text-muted-foreground">{t("dashboard.env_settings.schedule_retention")}</label>
                            <input
                                id="sched-keep"
                                type="number"
                                min={1}
                                max={100}
                                value={schedule.retention_count}
                                onChange={e => setSchedule({ ...schedule, retention_count: Number(e.target.value) })}
                                className="w-full bg-background border border-input rounded-xl px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/50"
                            />
                        </div>
                    </div>
                    <p className="text-xs text-muted-foreground leading-relaxed">{t("dashboard.env_settings.schedule_hint")}</p>
                    <div className="flex justify-end">
                        <button
                            onClick={handleSaveSchedule}
                            disabled={isSavingSchedule}
                            className="flex items-center gap-2 px-5 py-2 rounded-xl text-xs font-black bg-primary-button text-primary-foreground hover:opacity-90 disabled:opacity-50 transition-all"
                        >
                            {isSavingSchedule ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Clock className="w-4 h-4" />}
                            {t("dashboard.env_settings.schedule_save")}
                        </button>
                    </div>
                </div>
            )}

            {/* Security Warning Area */}
            <div className="p-6 bg-status-error/5 border border-status-error/10 rounded-3xl flex items-start gap-4">
                <AlertTriangle className="w-6 h-6 text-status-error shrink-0 mt-1" />
                <div className="space-y-1">
                    <p className="text-sm font-black text-status-error uppercase italic">{t("dashboard.env_settings.warning_title")}</p>
                    <p className="text-xs font-medium text-status-error/60 leading-relaxed">
                        {t("dashboard.env_settings.warning_body")}
                    </p>
                </div>
            </div>

            {/* Confirmation Modal — A3：改 Radix Dialog（焦點陷阱/Esc/焦點歸位/aria-modal） */}
            <Dialog open={!!showConfirm} onOpenChange={(o) => { if (!o) { setShowConfirm(null); setRestorePassword(""); } }}>
                <DialogContent
                    overlayClassName="bg-background/60 backdrop-blur-md"
                    className="bg-card border border-border rounded-3xl p-8 max-w-md w-full shadow-2xl space-y-6"
                >
                    <div className="flex items-center gap-4 text-status-error">
                        <div className="w-12 h-12 rounded-2xl bg-status-error/10 flex items-center justify-center">
                            <AlertTriangle className="w-6 h-6" />
                        </div>
                        <DialogTitle className="text-xl font-black uppercase italic tracking-tight">{t("dashboard.env_settings.confirm_restore_title")}</DialogTitle>
                    </div>

                    <DialogDescription className="text-sm text-foreground/80 leading-relaxed">
                        {/* I3：單鍵 <Trans> + filename placeholder，標點隨語系（不再硬寫「。」） */}
                        <Trans
                            i18nKey="dashboard.env_settings.confirm_restore_msg"
                            values={{ filename: showConfirm }}
                            components={{ b: <span className="font-mono font-bold text-primary underline underline-offset-4" /> }}
                        />
                    </DialogDescription>

                    {/* P1a：破壞性操作密碼閘 —— 對齊「清空我的資料」的 DA25 重輸密碼。 */}
                    <div className="space-y-1.5">
                        <label htmlFor="restore-pw" className="flex items-center gap-1.5 text-xs font-bold text-muted-foreground">
                            <Key className="w-3 h-3" /> {t("dashboard.env_settings.confirm_restore_password_label")}
                        </label>
                        <input
                            id="restore-pw"
                            type="password"
                            value={restorePassword}
                            onChange={(e) => setRestorePassword(e.target.value)}
                            placeholder={t("dashboard.env_settings.confirm_restore_password_placeholder")}
                            className="w-full bg-background border border-input rounded-xl px-4 py-2.5 text-sm outline-none focus:ring-2 focus:ring-primary/50 transition-all"
                        />
                    </div>

                    <div className="grid grid-cols-2 gap-3 pt-2">
                        <button
                            onClick={() => { setShowConfirm(null); setRestorePassword(""); }}
                            className="py-3 rounded-xl text-xs font-black uppercase tracking-widest border border-border hover:bg-accent transition-colors"
                        >
                            {t("dashboard.env_settings.cancel")}
                        </button>
                        <button
                            onClick={() => {
                                if (showConfirm) handleRestore(showConfirm, restorePassword);
                                setShowConfirm(null);
                                setRestorePassword("");
                            }}
                            disabled={!restorePassword}
                            className="bg-status-error py-3 text-white shadow-lg shadow-status-error/20 transition-all rounded-xl text-xs font-black uppercase tracking-widest hover:scale-105 active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100"
                        >
                            {t("dashboard.env_settings.confirm_restore_action")}
                        </button>
                    </div>
                </DialogContent>
            </Dialog>

            {/* P2（ADR-0108）：加密下載對話框——設密碼短語（≥8）後後端即時加密串流。 */}
            <Dialog open={!!downloadTarget} onOpenChange={(o) => { if (!o) { setDownloadTarget(null); setDownloadPassphrase(""); } }}>
                <DialogContent
                    overlayClassName="bg-background/60 backdrop-blur-md"
                    className="bg-card border border-border rounded-3xl p-8 max-w-md w-full shadow-2xl space-y-6"
                >
                    <div className="flex items-center gap-4 text-primary">
                        <div className="w-12 h-12 rounded-2xl bg-primary/10 flex items-center justify-center">
                            <Lock className="w-6 h-6" />
                        </div>
                        <DialogTitle className="text-xl font-black uppercase italic tracking-tight">{t("dashboard.env_settings.download_title")}</DialogTitle>
                    </div>

                    <DialogDescription className="text-sm text-foreground/80 leading-relaxed">
                        {t("dashboard.env_settings.download_body")}
                    </DialogDescription>

                    <div className="space-y-1.5">
                        <label htmlFor="download-pw" className="flex items-center gap-1.5 text-xs font-bold text-muted-foreground">
                            <Key className="w-3 h-3" /> {t("dashboard.env_settings.download_passphrase_label")}
                        </label>
                        <input
                            id="download-pw"
                            type="password"
                            value={downloadPassphrase}
                            onChange={(e) => setDownloadPassphrase(e.target.value)}
                            placeholder={t("dashboard.env_settings.download_passphrase_placeholder")}
                            className="w-full bg-background border border-input rounded-xl px-4 py-2.5 text-sm outline-none focus:ring-2 focus:ring-primary/50 transition-all"
                        />
                    </div>

                    <div className="grid grid-cols-2 gap-3 pt-2">
                        <button
                            onClick={() => { setDownloadTarget(null); setDownloadPassphrase(""); }}
                            className="py-3 rounded-xl text-xs font-black uppercase tracking-widest border border-border hover:bg-accent transition-colors"
                        >
                            {t("dashboard.env_settings.cancel")}
                        </button>
                        <button
                            onClick={() => {
                                if (downloadTarget) handleDownload(downloadTarget, downloadPassphrase);
                                setDownloadTarget(null);
                                setDownloadPassphrase("");
                            }}
                            disabled={downloadPassphrase.length < 12}
                            className="bg-primary-button py-3 text-primary-foreground shadow-lg shadow-primary/20 transition-all rounded-xl text-xs font-black uppercase tracking-widest hover:opacity-90 active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            {t("dashboard.env_settings.download_action")}
                        </button>
                    </div>
                </DialogContent>
            </Dialog>

            {/* ADR-0112：設定快照（可攜、零機密的設定匯入／匯出）*/}
            <SettingsSnapshotCard />
        </div>
    );
}

