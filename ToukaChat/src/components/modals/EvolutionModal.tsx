"use client";
import { useState } from "react";
import { Terminal, ShieldAlert, Key, Check, X, Loader2 } from "lucide-react";

import { SkillInstallRequest } from "@/lib/types";
import { errorMsg } from "@/lib/error_handler";
import { toast } from "sonner";
import { useTranslation } from "react-i18next";
import { useEvolution } from "@/hooks/useEvolution";
import {
    Dialog,
    DialogContent,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";

interface McpInstallModalProps {
    installRequest: SkillInstallRequest;
    onClose: () => void;
}

export default function McpInstallModal({ installRequest, onClose }: McpInstallModalProps) {
    const { t } = useTranslation();
    const [envVars, setEnvVars] = useState<Record<string, string>>({});
    const { isInstalling, confirmInstall } = useEvolution();

    const handleEnvChange = (key: string, value: string) => {
        setEnvVars(prev => ({ ...prev, [key]: value }));
    };

    const handleConfirm = async () => {
        try {
            const success = await confirmInstall(installRequest.id, envVars);
            if (success) {
                toast.success(t('modals.evolution_modal.install_success'));
                onClose();
            }
        } catch {
            toast.error(errorMsg("SYSTEM_ERROR"));
        }
    };


    return (
        // WCAG 2.1.2（A3）：手寫覆蓋層 → Radix Dialog（焦點陷阱/Esc/焦點歸位內建）
        <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
            <DialogContent
                overlayClassName="bg-background/80 backdrop-blur-sm"
                className="bg-card w-full max-w-2xl rounded-3xl border border-border shadow-2xl overflow-hidden flex flex-col max-h-[80vh]"
            >
                <div className="p-6 border-b border-border flex items-center justify-between shrink-0 bg-muted/20">
                    <div className="flex items-center gap-3">
                        <div className="p-2.5 bg-primary/10 text-primary rounded-xl">
                            <Terminal size={20} />
                        </div>
                        <div>
                            <DialogTitle className="text-xl font-black text-foreground">{installRequest.is_creation ? t('modals.evolution_modal.title_create') : t('modals.evolution_modal.title_evolve')}</DialogTitle>
                            <DialogDescription className="text-xs text-muted-foreground">{installRequest.is_creation ? t('modals.evolution_modal.desc_writing_tool') : t('modals.evolution_modal.desc_acquiring_capability')}{installRequest.name}</DialogDescription>
                        </div>
                    </div>
                    <button onClick={onClose} aria-label={t('modals.evolution_modal.close')} className="p-2 hover:bg-muted rounded-full transition-colors">
                        <X size={20} className="text-muted-foreground" />
                    </button>
                </div>

                <div className="p-6 overflow-y-auto custom-scrollbar flex-1 space-y-8">
                    <div className="p-4 bg-primary/5 border border-primary/20 rounded-2xl">
                        <p className="text-sm text-primary leading-relaxed">
                            {installRequest.is_creation
                                ? t('modals.evolution_modal.intro_create')
                                : t('modals.evolution_modal.intro_evolve')}
                        </p>
                    </div>

                    {/* Source Code Review (Phase 3 only) */}
                    {installRequest.source_code && (
                        <div className="space-y-4">
                            <div className="flex items-center justify-between">
                                <h4 className="text-sm font-bold flex items-center gap-2 text-foreground">
                                    <Terminal className="w-4 h-4 text-primary" /> {t('modals.evolution_modal.source_code_heading')}
                                </h4>
                                <span className="text-xs font-mono text-muted-foreground bg-muted px-2 py-0.5 rounded">Python 3.x</span>
                            </div>
                            <div className="relative group rounded-2xl overflow-hidden border border-border shadow-2xl">
                                <div className="absolute inset-0 bg-gradient-to-br from-primary/5 to-transparent pointer-events-none" />
                                <pre className="p-5 bg-black/90 text-status-success font-mono text-xs leading-relaxed overflow-x-auto max-h-80 custom-scrollbar">
                                    <code>{installRequest.source_code}</code>
                                </pre>
                                <div className="absolute top-4 right-4 flex gap-2">
                                    <div className="w-2 h-2 rounded-full bg-status-error/50" />
                                    <div className="w-2 h-2 rounded-full bg-status-warning/50" />
                                    <div className="w-2 h-2 rounded-full bg-status-success/50" />
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Step 1: Environment Variables */}
                    {installRequest.env_requirements && installRequest.env_requirements.length > 0 && (
                        <div className="space-y-4">
                            <h4 className="text-sm font-bold flex items-center gap-2 text-foreground">
                                <Key className="w-4 h-4 text-primary" /> {installRequest.is_creation ? t('modals.evolution_modal.step1_config_credentials') : t('modals.evolution_modal.step1_config_credentials_isolated')}
                            </h4>
                            <div className="space-y-3 p-4 bg-background border border-border rounded-2xl">
                                {installRequest.env_requirements.map((req: string) => (
                                    <div key={req} className="space-y-1.5">
                                        <label className="text-xs font-bold text-muted-foreground font-mono">{req}</label>
                                        <input 
                                            type="password"
                                            placeholder={t('modals.evolution_modal.input_placeholder', { req })}
                                            value={envVars[req] || ""}
                                            onChange={(e) => handleEnvChange(req, e.target.value)}
                                            className="w-full bg-muted border border-input rounded-xl px-4 py-2.5 text-sm outline-none focus:ring-2 focus:ring-primary transition-all text-foreground"
                                        />
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}

                    {/* Step 2: Command Preview — 僅安裝事件（有 launch command＋args）渲染。
                        演化部署（is_creation）於核准後才組出啟動指令，審核標的是上方
                        source_code，故此區塊略過（亦避免 args 為空時崩潰）。 */}
                    {installRequest.args && (
                    <div className="space-y-4">
                        <h4 className="text-sm font-bold flex items-center gap-2 text-foreground">
                            <ShieldAlert className="w-4 h-4 text-status-warning" /> {installRequest.is_creation ? t('modals.evolution_modal.step2_audit_deps') : t('modals.evolution_modal.step2_audit_command')}
                        </h4>
                        
                        <div className="p-4 bg-muted border border-border rounded-2xl space-y-3">
                            <div className="text-xs uppercase font-black tracking-widest text-muted-foreground">{t('modals.evolution_modal.launch_command_label')}</div>
                            <div className="p-3 bg-black/90 text-status-success font-mono text-sm rounded-xl overflow-x-auto whitespace-nowrap">
                                <span className="opacity-50 select-none mr-2">$</span>
                                {installRequest.command} {installRequest.args.join(" ")}
                            </div>
                        </div>
                    </div>
                    )}
                </div>

                <div className="p-6 border-t border-border flex items-center justify-end gap-3 bg-muted shrink-0">
                    <button 
                        onClick={onClose}
                        disabled={isInstalling}
                        className="px-6 py-2.5 rounded-xl font-bold bg-muted hover:bg-accent text-muted-foreground transition-colors text-sm"
                    >
                        {t('modals.evolution_modal.dismiss')}
                    </button>
                    <button
                        onClick={handleConfirm}
                        disabled={isInstalling}
                        className="px-6 py-2.5 rounded-xl font-bold bg-primary-button hover:bg-primary-button/90 text-foreground shadow-lg shadow-primary/20 transition-all text-sm flex items-center gap-2"
                    >
                        {isInstalling ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check size={16} />}
                        {t('modals.evolution_modal.confirm_install')}
                    </button>
                </div>
            </DialogContent>
        </Dialog>
    );
}
