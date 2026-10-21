import React, { useState, useEffect } from "react";
import { cn } from "@/lib/utils";
import { useArtifacts } from "@/hooks/useArtifacts";
import ArtifactView from "./artifacts/ArtifactView";
import ProjectSidebarView from "./projects/ProjectSidebarView";
import { 
    LayoutDashboard, Settings2,
    X, Folder, Target, Bug
} from "lucide-react";
import MissionBoard from "./artifacts/MissionBoard";
import { SystemMonitoringView } from "./sidebar/SystemMonitoringView";
import { DebugMonitoringView } from "./sidebar/DebugMonitoringView";
import { DegradedBanner } from "./sidebar/DegradedBanner";
import { MemoryNoticeBar } from "./sidebar/MemoryNoticeBar";
import { SystemNoticeBar } from "./sidebar/SystemNoticeBar";
import { IsolationBanner } from "./sidebar/IsolationBanner";
import { useSystemStatus } from "@/lib/SystemStatusContext";
import { useTranslation } from "react-i18next";

interface RightSidebarProps {
    isOpen: boolean;
    onClose: () => void;
    isIDE?: boolean;
}

function RightSidebar({ isOpen, onClose, isIDE }: RightSidebarProps) {
    const { t } = useTranslation();
    const { activeTab, setActiveTab, artifacts } = useArtifacts();
    const { status: systemStatus } = useSystemStatus();
    const [mounted, setMounted] = useState(false);

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setMounted(true);
    }, []);

    if (!mounted) return null;

    return (
        // WCAG（A5 axe landmark-unique）：兩個 aside 地標需可辨識名稱
        <aside
            aria-label={t('layout.right_sidebar.aria_system_panel')}
            className={cn(
                "bg-sidebar border-l border-border flex flex-col shrink-0 font-sans h-full w-full relative transition-all duration-300 ease-in-out overflow-hidden shadow-2xl",
                isOpen ? "opacity-100 translate-x-0" : "opacity-0 translate-x-full"
            )}
        >
            <div className={cn(
                "flex flex-col h-full w-full min-w-0 overflow-hidden transition-all duration-300",
                isOpen ? "delay-100" : ""
            )}>
                {/* Tabs Header */}
                <div className={cn(
                    "flex border-b border-border bg-sidebar sticky top-0 z-20 shrink-0 w-full overflow-hidden",
                    isIDE ? "h-10" : "h-16"
                )}>
                    <button 
                        onClick={() => setActiveTab('artifacts')}
                        className={cn(
                            "flex-1 flex items-center justify-center gap-0.5 text-xs font-black tracking-tight transition-all px-0.5 md:px-1 min-w-0 h-full",
                            activeTab === 'artifacts' 
                                ? "text-primary border-b-2 border-primary bg-background" 
                                : "text-muted-foreground hover:text-foreground"
                        )}
                    >
                        <LayoutDashboard className="w-3.5 h-3.5 shrink-0" />
                        <span className="truncate">ARTIFACTS {artifacts.length > 0 && `(${artifacts.length})`}</span>
                    </button>
                    <button 
                        onClick={() => setActiveTab('mission')}
                        className={cn(
                            "flex-1 flex items-center justify-center gap-0.5 text-xs font-black tracking-tight transition-all px-0.5 md:px-1 min-w-0 h-full",
                            activeTab === 'mission' 
                                ? "text-primary border-b-2 border-primary bg-background" 
                                : "text-muted-foreground hover:text-foreground"
                        )}
                    >
                        <Target className="w-3.5 h-3.5 shrink-0" />
                        <span className="truncate">MISSION</span>
                    </button>
                    <button 
                        onClick={() => setActiveTab('project')}
                        className={cn(
                            "flex-1 flex items-center justify-center gap-0.5 text-xs font-black tracking-tight transition-all px-0.5 md:px-1 min-w-0 h-full",
                            activeTab === 'project' 
                                ? "text-primary border-b-2 border-primary bg-background" 
                                : "text-muted-foreground hover:text-foreground"
                        )}
                    >
                        <Folder className="w-3.5 h-3.5 shrink-0" />
                        <span className="truncate">PROJECT</span>
                    </button>
                    <button 
                        onClick={() => setActiveTab('system')}
                        className={cn(
                            "flex-1 flex items-center justify-center gap-0.5 text-xs font-black tracking-tight transition-all px-0.5 md:px-1 min-w-0 h-full",
                            activeTab === 'system' 
                                ? "text-primary border-b-2 border-primary bg-background" 
                                : "text-muted-foreground hover:text-foreground"
                        )}
                    >
                        <Settings2 className="w-3.5 h-3.5 shrink-0" />
                        <span className="truncate">SYSTEM</span>
                    </button>
                    <button 
                        onClick={() => setActiveTab('debug')}
                        className={cn(
                            "flex-1 flex items-center justify-center gap-0.5 text-xs font-black tracking-tight transition-all px-0.5 md:px-1 min-w-0 h-full",
                            activeTab === 'debug' 
                                ? "text-primary border-b-2 border-primary bg-background" 
                                : "text-muted-foreground hover:text-foreground"
                        )}
                    >
                        <Bug className="w-3.5 h-3.5 shrink-0" />
                        <span className="truncate">DEBUG</span>
                    </button>
                    <button onClick={onClose} aria-label={t('layout.right_sidebar.aria_close_panel')} className="px-3 text-muted-foreground hover:text-foreground border-l border-border shrink-0 h-full">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {/* P5b polish：系統降級常駐橫幅——跨分頁可見，記憶系統未載入時提示 */}
                <DegradedBanner
                    degraded={systemStatus.systemDegraded}
                    reason={systemStatus.degradedReason}
                />
                {/* 記憶提示（例如舊記憶檔未搬入）：只給人看，不是降級 */}
                <MemoryNoticeBar notices={systemStatus.memoryNotices} />
                {/* 系統提示（/health 的 system_notices）：設定讀不到而停掉的功能 */}
                <SystemNoticeBar notices={systemStatus.systemNotices} />
                {/* 隔離低於預期、埠沒擋、自癒額度用完（GET /v1/isolation） */}
                <IsolationBanner />

                <div className="flex-1 overflow-y-auto min-h-0 w-full custom-scrollbar overflow-x-hidden">
                    {activeTab === 'artifacts' ? (
                        <div className="h-full w-full min-w-0">
                            <ArtifactView />
                        </div>
                    ) : activeTab === 'mission' ? (
                        <div className="h-full w-full min-w-0">
                            <MissionBoard />
                        </div>
                    ) : activeTab === 'project' ? (
                        <div className="h-full w-full min-w-0">
                            <ProjectSidebarView />
                        </div>
                    ) : activeTab === 'system' ? (
                        <SystemMonitoringView isActive={isOpen && activeTab === "system"} />
                    ) : (
                        <DebugMonitoringView />
                    )}
                </div>
            </div>
        </aside>
    );
}

export default React.memo(RightSidebar);
