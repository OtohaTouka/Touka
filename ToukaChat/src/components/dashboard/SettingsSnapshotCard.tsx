"use client";

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Package, Upload, Download, ShieldAlert, Loader2, Lock, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { cn } from "@/lib/utils";
import { logSSOTError, errorMsg, getErrorMessage } from "@/lib/error_handler";
import {
    Dialog,
    DialogContent,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";

// ADR-0112 匯入端點回傳摘要（dry_run 預覽與實際套用共用）。
interface ImportResult {
    dry_run: boolean;
    mode: string;
    applied: string[];
    warnings: string[];
    skipped_secrets: string[];
    requires_restart: string[];
    pre_import_backup?: { message?: string } | null;
}

type Mode = "merge" | "replace";

/** ADR-0112 設定快照（Config Snapshot）匯出／匯入卡——admin only，置於 EnvSettings。 */
export function SettingsSnapshotCard() {
    const { t } = useTranslation();
    const [exporting, setExporting] = useState(false);
    const [encryptExport, setEncryptExport] = useState(false);
    const [exportPass, setExportPass] = useState("");

    const [snapshot, setSnapshot] = useState<Record<string, unknown> | null>(null);
    const [fileName, setFileName] = useState("");
    const [mode, setMode] = useState<Mode>("merge");
    const [preview, setPreview] = useState<ImportResult | null>(null);
    const [busy, setBusy] = useState(false);
    const [confirmOpen, setConfirmOpen] = useState(false);
    const [password, setPassword] = useState("");
    const [result, setResult] = useState<ImportResult | null>(null);

    const proxyUrl = getActiveProxyUrl();

    // ── 匯出 ──
    const doExport = async () => {
        if (encryptExport && exportPass.length < 12) return;
        setExporting(true);
        try {
            const res = await fetchTouka(proxyUrl, "/v1/system/settings/export", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(
                    encryptExport ? { encrypt: true, passphrase: exportPass } : { encrypt: false },
                ),
            });
            if (!res.ok) {
                toast.error(await getErrorMessage(res, "SYSTEM_ERROR"));
                return;
            }
            const blob = encryptExport
                ? await res.blob()
                : new Blob([JSON.stringify(await res.json(), null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = encryptExport ? "touka-settings_encrypted.zip" : "touka-settings.json";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            toast.success(t("dashboard.settings_snapshot.export_success"));
        } catch (e) {
            logSSOTError(e, "SettingsSnapshotCard:export");
            toast.error(errorMsg("SYSTEM_ERROR"));
        } finally {
            setExporting(false);
        }
    };

    // ── 匯入：選檔 → 自動乾跑預覽 ──
    const runPreview = async (snap: Record<string, unknown>, m: Mode) => {
        setBusy(true);
        try {
            const res = await fetchTouka(proxyUrl, "/v1/system/settings/import", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ snapshot: snap, mode: m, dry_run: true }),
            });
            if (!res.ok) {
                toast.error(await getErrorMessage(res, "VALIDATION_ERROR"));
                setPreview(null);
                return;
            }
            setPreview(await res.json());
        } catch (e) {
            logSSOTError(e, "SettingsSnapshotCard:preview");
            setPreview(null);  // 預覽失敗即清除，避免顯示與當前模式不符的陳舊摘要
            toast.error(errorMsg("SYSTEM_ERROR"));
        } finally {
            setBusy(false);
        }
    };

    const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        e.target.value = ""; // 允許重選同檔
        if (!file) return;
        setResult(null);
        try {
            const obj = JSON.parse(await file.text());
            setSnapshot(obj);
            setFileName(file.name);
            await runPreview(obj, mode);
        } catch {
            toast.error(t("dashboard.settings_snapshot.invalid_file"));
            resetImport();
        }
    };

    const changeMode = (m: Mode) => {
        setMode(m);
        setResult(null);  // 套用後再改模式 → 清除舊結果，重新進入「預覽→套用」循環
        if (snapshot) runPreview(snapshot, m);
    };

    const resetImport = () => {
        setSnapshot(null);
        setFileName("");
        setPreview(null);
        setResult(null);
    };

    // ── 套用（密碼確認後）──
    const doApply = async () => {
        if (!snapshot) return;
        setBusy(true);
        try {
            const res = await fetchTouka(proxyUrl, "/v1/system/settings/import", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ snapshot, mode, dry_run: false, current_password: password }),
            });
            if (!res.ok) {
                toast.error(await getErrorMessage(res, "SYSTEM_ERROR"));
                return;
            }
            setResult(await res.json());
            setConfirmOpen(false);
            setPassword("");
            toast.success(t("dashboard.settings_snapshot.import_success"));
        } catch (e) {
            logSSOTError(e, "SettingsSnapshotCard:apply");
            toast.error(errorMsg("SYSTEM_ERROR"));
        } finally {
            setBusy(false);
        }
    };

    const summary = result ?? preview;

    return (
        <div className="rounded-3xl border border-border bg-muted/10 p-6 space-y-6">
            <div className="flex items-center gap-2">
                <Package className="w-5 h-5 text-primary" />
                <h4 className="text-sm font-black uppercase tracking-tight text-foreground">
                    {t("dashboard.settings_snapshot.title")}
                </h4>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
                {t("dashboard.settings_snapshot.intro")}
            </p>

            {/* 匯出 */}
            <div className="space-y-3 rounded-2xl border border-border bg-background/50 p-4">
                <div className="flex items-center justify-between gap-3">
                    <span className="text-xs font-bold text-foreground">
                        {t("dashboard.settings_snapshot.export_heading")}
                    </span>
                    <button
                        onClick={doExport}
                        disabled={exporting || (encryptExport && exportPass.length < 12)}
                        className={cn(
                            "flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-black uppercase tracking-widest transition-all",
                            exporting || (encryptExport && exportPass.length < 12)
                                ? "bg-muted text-muted-foreground cursor-not-allowed"
                                : "bg-primary-button text-primary-foreground hover:opacity-90 shadow-sm",
                        )}
                    >
                        {exporting ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                        {t("dashboard.settings_snapshot.export_button")}
                    </button>
                </div>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                    <input
                        type="checkbox"
                        checked={encryptExport}
                        onChange={(e) => setEncryptExport(e.target.checked)}
                        className="accent-primary"
                    />
                    <Lock size={12} /> {t("dashboard.settings_snapshot.encrypt_label")}
                </label>
                {encryptExport && (
                    <input
                        type="password"
                        value={exportPass}
                        onChange={(e) => setExportPass(e.target.value)}
                        placeholder={t("dashboard.settings_snapshot.passphrase_placeholder")}
                        className="w-full rounded-xl border border-input bg-muted/30 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/30"
                    />
                )}
            </div>

            {/* 匯入 */}
            <div className="space-y-3 rounded-2xl border border-border bg-background/50 p-4">
                <span className="text-xs font-bold text-foreground">
                    {t("dashboard.settings_snapshot.import_heading")}
                </span>
                {!snapshot ? (
                    <label className="flex cursor-pointer items-center justify-center gap-2 rounded-xl border-2 border-dashed border-border py-4 text-xs font-bold text-muted-foreground transition-all hover:border-primary/40 hover:text-primary">
                        <Upload size={14} /> {t("dashboard.settings_snapshot.choose_file")}
                        <input type="file" accept="application/json,.json" onChange={onFile} className="hidden" />
                    </label>
                ) : (
                    <div className="space-y-3">
                        <div className="flex items-center justify-between gap-2">
                            <span className="truncate font-mono text-xs text-foreground">{fileName}</span>
                            <button
                                onClick={resetImport}
                                className="flex items-center gap-1 text-xs font-bold text-muted-foreground hover:text-foreground"
                            >
                                <RotateCcw size={12} /> {t("dashboard.settings_snapshot.reset")}
                            </button>
                        </div>

                        {/* 模式 */}
                        <div className="flex rounded-xl border border-border bg-background p-1">
                            {(["merge", "replace"] as Mode[]).map((m) => (
                                <button
                                    key={m}
                                    onClick={() => changeMode(m)}
                                    aria-pressed={mode === m}
                                    className={cn(
                                        "flex-1 rounded-lg px-3 py-1.5 text-xs font-black transition-all",
                                        mode === m
                                            ? "bg-primary-button text-primary-foreground shadow-sm"
                                            : "text-muted-foreground hover:text-foreground",
                                    )}
                                >
                                    {t(`dashboard.settings_snapshot.mode_${m}`)}
                                </button>
                            ))}
                        </div>
                        {mode === "replace" && (
                            <p className="flex items-start gap-2 rounded-xl border border-status-warning/30 bg-status-warning/10 p-3 text-xs font-bold text-status-warning">
                                <ShieldAlert size={14} className="mt-0.5 shrink-0" />
                                {t("dashboard.settings_snapshot.replace_warning")}
                            </p>
                        )}

                        {/* 預覽／結果摘要 */}
                        {busy && !summary && (
                            <div className="flex items-center gap-2 text-xs text-muted-foreground">
                                <Loader2 size={14} className="animate-spin" /> {t("dashboard.settings_snapshot.previewing")}
                            </div>
                        )}
                        {summary && <SnapshotSummary summary={summary} t={t} />}

                        {!result && (
                            <button
                                onClick={() => setConfirmOpen(true)}
                                disabled={!preview || busy}
                                className={cn(
                                    "w-full rounded-xl px-4 py-2 text-xs font-black uppercase tracking-widest transition-all",
                                    !preview || busy
                                        ? "bg-muted text-muted-foreground cursor-not-allowed"
                                        : "bg-primary-button text-primary-foreground hover:opacity-90 shadow-sm",
                                )}
                            >
                                {t("dashboard.settings_snapshot.apply_button")}
                            </button>
                        )}
                    </div>
                )}
            </div>

            {/* 套用確認（密碼重輸）*/}
            <Dialog open={confirmOpen} onOpenChange={(o) => { if (!busy) setConfirmOpen(o); }}>
                <DialogContent className="max-w-md">
                    <DialogTitle className="flex items-center gap-2 text-base font-black">
                        <ShieldAlert className="w-5 h-5 text-status-warning" />
                        {t("dashboard.settings_snapshot.confirm_title")}
                    </DialogTitle>
                    <DialogDescription className="text-xs leading-relaxed">
                        {t("dashboard.settings_snapshot.confirm_body")}
                    </DialogDescription>
                    <input
                        type="password"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        placeholder={t("dashboard.settings_snapshot.password_placeholder")}
                        className="mt-2 w-full rounded-xl border border-input bg-muted/30 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/30"
                    />
                    <div className="mt-2 flex justify-end gap-2">
                        <button
                            onClick={() => { setConfirmOpen(false); setPassword(""); }}
                            disabled={busy}
                            className="rounded-xl px-4 py-2 text-xs font-bold text-muted-foreground hover:text-foreground"
                        >
                            {t("dashboard.settings_snapshot.cancel")}
                        </button>
                        <button
                            onClick={doApply}
                            disabled={busy || !password}
                            className={cn(
                                "flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-black uppercase tracking-widest transition-all",
                                busy || !password
                                    ? "bg-muted text-muted-foreground cursor-not-allowed"
                                    : "bg-destructive text-white hover:opacity-90",
                            )}
                        >
                            {busy ? <Loader2 size={14} className="animate-spin" /> : null}
                            {t("dashboard.settings_snapshot.confirm_apply")}
                        </button>
                    </div>
                </DialogContent>
            </Dialog>
        </div>
    );
}

// 預覽／結果摘要：套用區、警告、待補機密、需重啟。
function SnapshotSummary({ summary, t }: { summary: ImportResult; t: (k: string, o?: Record<string, unknown>) => string }) {
    return (
        <div className="space-y-2 rounded-xl border border-border bg-muted/20 p-3 text-xs">
            {summary.pre_import_backup && (
                <p className="font-bold text-status-success">
                    {t("dashboard.settings_snapshot.backup_made")}
                </p>
            )}
            <p className="text-foreground">
                {t("dashboard.settings_snapshot.applied_count", { count: summary.applied.length })}
                {summary.applied.length > 0 && (
                    <span className="text-muted-foreground"> — {summary.applied.join(", ")}</span>
                )}
            </p>
            {summary.requires_restart.length > 0 && (
                <p className="font-bold text-status-warning">
                    {t("dashboard.settings_snapshot.requires_restart", { sections: summary.requires_restart.join(", ") })}
                </p>
            )}
            {summary.skipped_secrets.length > 0 && (
                <p className="text-muted-foreground">
                    {t("dashboard.settings_snapshot.skipped_secrets", { items: summary.skipped_secrets.join(", ") })}
                </p>
            )}
            {summary.warnings.length > 0 && (
                <ul className="list-disc space-y-0.5 pl-4 text-muted-foreground">
                    {summary.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
            )}
        </div>
    );
}
