// src/components/dashboard/PulseDashboard.tsx
"use client";

import { useTranslation } from "react-i18next";
import { Activity } from "lucide-react";
import { HeartbeatScheduler } from "./HeartbeatScheduler";
import { HeartbeatWhitelist } from "./HeartbeatWhitelist";

export function PulseDashboard() {
    const { t } = useTranslation();

    return (
        <div className="flex flex-col h-full bg-background animate-in fade-in duration-500 overflow-hidden font-sans">
            <header className="px-8 py-6 border-b border-border bg-sidebar shrink-0">
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                    <div className="flex items-center gap-4">
                        <div className="p-3 bg-primary/10 rounded-2xl">
                            <Activity className="w-6 h-6 text-primary" />
                        </div>
                        <div>
                            <h2 className="text-2xl font-black text-foreground uppercase tracking-tight italic">
                                {t('dashboard.pulse_dashboard.title')} <span className="text-xs font-medium not-italic bg-primary/10 text-primary px-2 py-0.5 rounded-full border border-primary/20">Pulse Control</span>
                            </h2>
                            <p className="text-sm font-bold uppercase tracking-widest text-muted-foreground opacity-60">{t('dashboard.pulse_dashboard.subtitle')}</p>
                        </div>
                    </div>
                </div>
            </header>

            <div className="flex-1 overflow-y-auto p-8 custom-scrollbar">
                <div className="max-w-6xl mx-auto space-y-8">
                    <div className="animate-in fade-in slide-in-from-bottom-4 duration-500 space-y-8">
                        <HeartbeatWhitelist />
                        <HeartbeatScheduler />
                    </div>
                </div>
            </div>
        </div>
    );
}
