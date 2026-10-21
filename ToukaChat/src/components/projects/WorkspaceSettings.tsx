"use client";

import { useState, useEffect } from 'react';
import { useTranslation, Trans } from "react-i18next";
import { Settings, Save, RefreshCw, CheckCircle2, AlertCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { errorMsg } from '@/lib/error_handler';
import { useAuth } from '@/contexts/AuthContext';

import { useProjects } from '@/hooks/useProjects';

export default function WorkspaceSettings() {
    const { t } = useTranslation();
    const { user } = useAuth();
    const { fetchWorkspaceRoot, saveWorkspaceRoot } = useProjects();
    const [path, setPath] = useState("");
    const [isSaving, setIsSaving] = useState(false);
    const [status, setStatus] = useState<'idle' | 'success' | 'error'>('idle');
    const [message, setMessage] = useState("");

    useEffect(() => {
        const init = async () => {
            const root = await fetchWorkspaceRoot();
            setPath(root);
        };
        init();
    }, [fetchWorkspaceRoot]);

    const handleSave = async () => {
        setIsSaving(true);
        setStatus('idle');
        try {
            const success = await saveWorkspaceRoot(path);
            if (success) {
                setStatus('success');
                setMessage(t('projects.workspace_settings.host_path_updated'));
                setTimeout(() => setStatus('idle'), 3000);
            } else {
                throw new Error(errorMsg("CONFIG_SAVE_FAILED"));
            }
        } catch (e) {
            setStatus('error');
            setMessage((e instanceof Error ? e.message : "") || errorMsg("SYSTEM_ERROR"));
        } finally {
            setIsSaving(false);
        }
    };

    // M-fix: the workspace root is a global, admin-only setting (POST is gated by
    // _require_admin_role). Hide the whole section from non-admins so they don't see
    // an editable field they can never save. (All hooks run above this early return.)
    if (user?.role !== 'admin') return null;

    return (
        <div className="flex flex-col gap-3 p-3 bg-muted/30 rounded-xl border border-border mt-4">
            <div className="flex items-center gap-2">
                <Settings className="w-4 h-4 text-muted-foreground" />
                <span className="text-xs font-black uppercase tracking-widest text-muted-foreground">{t('projects.workspace_settings.section_title')}</span>
            </div>
            
            <div className="flex flex-col gap-1.5">
                <label className="text-xs text-muted-foreground font-medium ml-1">{t('projects.workspace_settings.host_absolute_path_label')}</label>
                <div className="flex gap-2">
                    <input 
                        type="text" 
                        value={path}
                        onChange={(e) => setPath(e.target.value)}
                        placeholder={t('projects.workspace_settings.host_path_placeholder')}
                        className="flex-1 bg-background border border-input rounded-lg px-2 py-1.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-primary/30"
                    />
                    <Button
                        size="icon"
                        aria-label={t('projects.workspace_settings.save_host_path_aria')}
                        className="h-8 w-8 shrink-0 rounded-lg"
                        onClick={handleSave}
                        disabled={isSaving || !path}
                    >
                        {isSaving ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                    </Button>
                </div>
            </div>

            {status !== 'idle' && (
                <div className={cn(
                    "flex items-center gap-1.5 p-1.5 rounded-md text-xs font-bold animate-in fade-in slide-in-from-top-1",
                    status === 'success' ? "bg-primary/10 text-primary" : "bg-destructive/10 text-destructive"
                )}>
                    {status === 'success' ? <CheckCircle2 className="w-3 h-3" /> : <AlertCircle className="w-3 h-3" />}
                    {message}
                </div>
            )}

            <p className="text-xs text-muted-foreground leading-relaxed italic border-t border-border/50 pt-2 px-1">
                <Trans i18nKey="projects.workspace_settings.docker_mount_note">
                    注意：此路徑對應 Docker 內的 <code>/app/workspace</code> 掛載點。若您更改了主機存放目錄，請確保 <code>docker-compose.yml</code> 亦同步更新。
                </Trans>
            </p>
        </div>
    );
}
