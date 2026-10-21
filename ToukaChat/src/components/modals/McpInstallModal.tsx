"use client";
import { useState } from "react";
import { Terminal, ShieldAlert, Key, Check, X, Loader2 } from "lucide-react";
import { useTranslation, Trans } from "react-i18next";

import { McpHubServer } from "@/lib/types";
import {
    Dialog,
    DialogContent,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";

interface McpInstallModalProps {
    isOpen: boolean;
    server: McpHubServer | null;
    onClose: () => void;
    onConfirm: (envVars: Record<string, string>) => Promise<void>;
}

export function McpInstallModal({ 
    isOpen, 
    server, 
    onClose, 
    onConfirm 
}: McpInstallModalProps) {
    const { t } = useTranslation();
    const [envVars, setEnvVars] = useState<Record<string, string>>({});
    const [isInstalling, setIsInstalling] = useState(false);

    if (!server) return null;

    // remote 條目（ADR-0192）帶 url、無本地啟動指令；風險性質與 stdio 不同，文案分寫。
    const isRemote = Boolean(server.url);

    const handleEnvChange = (key: string, value: string) => {
        setEnvVars(prev => ({ ...prev, [key]: value }));
    };

    const handleConfirm = async () => {
        setIsInstalling(true);
        try {
            await onConfirm(envVars);
        } finally {
            setIsInstalling(false);
        }
    };

    return (
        // WCAG 2.1.2（P3）：手寫覆蓋層 → Radix Dialog（焦點陷阱/Esc/焦點歸位/aria-modal 內建）
        <Dialog open={isOpen} onOpenChange={(open) => { if (!open) onClose(); }}>
            <DialogContent
                overlayClassName="bg-background/80 backdrop-blur-sm"
                className="bg-card w-full max-w-2xl rounded-3xl border border-border shadow-2xl overflow-hidden flex flex-col max-h-[90vh]"
            >
                <div className="p-6 border-b border-border flex items-center justify-between bg-muted/20">
                    <div className="flex items-center gap-3">
                        <div className="p-2.5 bg-primary/10 text-primary rounded-xl">
                            <Terminal size={20} />
                        </div>
                        <div>
                            <DialogTitle className="text-xl font-black text-foreground">{t('modals.mcp_install_modal.title')}</DialogTitle>
                            <DialogDescription className="text-xs text-muted-foreground">{server.name} {t('modals.mcp_install_modal.mcp_hub_source')}</DialogDescription>
                        </div>
                    </div>
                    <button onClick={onClose} aria-label={t('modals.mcp_install_modal.close')} className="p-2 hover:bg-muted rounded-full transition-colors">
                        <X size={20} className="text-muted-foreground" />
                    </button>
                </div>

                <div className="p-6 overflow-y-auto custom-scrollbar flex-1 space-y-6">
                    <div className="space-y-4">
                        <div className="flex items-start gap-4 p-4 bg-destructive/10 border border-destructive/30 rounded-2xl text-destructive">
                            <ShieldAlert className="w-5 h-5 shrink-0 mt-0.5" />
                            <p className="text-xs leading-relaxed">
                                {/* remote 條目不在本機起任何進程，風險是憑證外送——沿用 stdio 文案等於對使用者說謊（ADR-0192）。 */}
                                {isRemote ? (
                                    <Trans i18nKey="modals.mcp_install_modal.security_warning_remote">
                                        此為遠端伺服器：不會在本機執行任何程序，但 agent 連線時會<strong>把你填入的憑證送到下列外部端點</strong>。請確認你信任該端點的營運者。
                                    </Trans>
                                ) : (
                                    <Trans i18nKey="modals.mcp_install_modal.security_warning">
                                        安裝後將設定一個<strong>會在伺服器上執行的外部指令程序</strong>（command + args），agent 連線使用時即會啟動。請僅安裝你信任的來源。
                                    </Trans>
                                )}
                            </p>
                        </div>

                        {/* Environment Variables */}
                        {server.env_requirements && server.env_requirements.length > 0 && (
                            <div className="space-y-4 pt-2">
                                <h4 className="text-sm font-bold flex items-center gap-2 text-foreground">
                                    <Key className="w-4 h-4 text-primary" /> {t('modals.mcp_install_modal.configure_api_credentials')}
                                </h4>
                                <div className="space-y-3 p-4 bg-background border border-border rounded-2xl">
                                    {server.env_requirements.map((req: string) => (
                                        <div key={req} className="space-y-1.5">
                                            <label className="text-xs font-bold text-muted-foreground font-mono">{req}</label>
                                            <input 
                                                type="password"
                                                placeholder={t('modals.mcp_install_modal.env_input_placeholder', { req })}
                                                value={envVars[req] || ""}
                                                onChange={(e) => handleEnvChange(req, e.target.value)}
                                                className="w-full bg-muted border border-input rounded-xl px-4 py-2.5 text-sm outline-none focus:border-primary transition-all text-foreground"
                                            />
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}

                        {/* Command Preview */}
                        <div className="space-y-3">
                            <h4 className="text-sm font-bold flex items-center gap-2 text-foreground">
                                <ShieldAlert className="w-4 h-4 text-status-warning" /> {t(isRemote ? 'modals.mcp_install_modal.endpoint_review' : 'modals.mcp_install_modal.command_review')}
                            </h4>
                            
                            <div className="p-4 bg-muted/50 border border-border rounded-2xl space-y-2">
                                <div className="p-3 bg-black/90 text-status-success font-mono text-sm rounded-xl overflow-x-auto">
                                    <span className="opacity-50 select-none mr-2">{isRemote ? "→" : "$"}</span>
                                    {isRemote ? server.url : `${server.command ?? ""} ${(server.args ?? []).join(" ")}`}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div className="p-6 border-t border-border flex items-center justify-end gap-3 bg-muted/10">
                    <button 
                        onClick={onClose}
                        disabled={isInstalling}
                        className="px-6 py-2.5 rounded-xl font-bold bg-muted hover:bg-accent text-muted-foreground transition-colors text-sm"
                    >
                        {t('modals.mcp_install_modal.button_later')}
                    </button>
                    <button 
                        onClick={handleConfirm}
                        disabled={isInstalling}
                        className="px-6 py-2.5 rounded-xl font-bold bg-primary-button hover:bg-primary-button/90 text-white shadow-lg shadow-primary/20 transition-all text-sm flex items-center gap-2"
                    >
                        {isInstalling ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check size={16} />}
                        {t('modals.mcp_install_modal.button_install')}
                    </button>
                </div>
            </DialogContent>
        </Dialog>
    );
}
