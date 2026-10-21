// src/components/LeftSidebar.tsx
"use client";

import React, { useState, useEffect } from 'react';
import { useTranslation } from "react-i18next";
import { Button } from '@/components/ui/button';
import { useSystemStatus } from '@/lib/SystemStatusContext';
import { hasMemoryWarning } from '@/lib/memoryNotices';
import { cn } from '@/lib/utils';
import { useChat } from '@/lib/providers/ChatProvider';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { useProjects } from '@/lib/providers/ProjectProvider';
import { PlusCircle, VenetianMask } from "lucide-react";

// Hooks
import { useSessionGroups } from '@/hooks/useSessionGroups';
import { useSessionActions } from '@/hooks/useSessionActions';

// Components
import { SidebarIdentity } from './sidebar/left/SidebarIdentity';
import { NavigationArea } from './sidebar/left/NavigationArea';
import { SessionItem } from './sidebar/left/SessionItem';

interface LeftSidebarProps {
    onStudioClick?: () => void;
    onChatClick?: () => void;
    onPulseClick?: () => void;
    onVaultClick?: () => void;
    onSettingsClick?: () => void;
    onSoulClick?: () => void;
    activeMode?: "chat" | "soul" | "studio" | "pulse" | "vault" | "settings" | "agent" | "projects";
    onProjectsClick?: () => void;
    isCollapsed?: boolean;
}

function LeftSidebar({ 
    onSoulClick, onStudioClick, onChatClick, onPulseClick, onVaultClick, onSettingsClick, onProjectsClick,
    activeMode = "chat",
    isCollapsed = false
}: LeftSidebarProps) {
    const { t } = useTranslation();
    const { status: sysStatus } = useSystemStatus();
    const { sessions } = useSessionList();
    const {
        activeSessionId,
        handleNewSession,
        handleSwitchSession,
        handleSetHome,
        handleSetProject,
        handleUnsetHome,
        streamingSessionIds
    } = useChat();
    const { projects } = useProjects();

    const [searchQuery] = useState("");
    const [mounted, setMounted] = useState(false);
    const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});

    // Use refactored logic (SOLID: SRP/DIP)
    const { sessionGroups } = useSessionGroups({
        sessions, projects, activeMode, searchQuery
    });

    const sessionActions = useSessionActions();

    // 僅用於標記掛載狀態
    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setMounted(true);
    }, []);

    const toggleGroup = (group: string) => {
        setCollapsedGroups(prev => ({ ...prev, [group]: !prev[group] }));
    };

    return (
        <div className={cn(
            "relative h-full flex flex-col transition-all duration-300 ease-in-out overflow-hidden shrink-0",
            isCollapsed ? "w-0 opacity-0 invisible" : "w-full opacity-100 visible"
        )}>
            {/* WCAG（A5 axe landmark-unique）：兩個 aside 地標需可辨識名稱 */}
            <aside aria-label={t('layout.left_sidebar.nav_aria_label')} className="h-full bg-sidebar flex flex-col border-r border-border overflow-hidden w-full">
                <div className="py-4 flex flex-col gap-6 h-full relative transition-all duration-300 w-full min-w-0 px-4">
                    
                    {/* Identity (SRP: SidebarIdentity) */}
                    <SidebarIdentity mode={sysStatus.mode} checkState={sysStatus.checkState}
                        memoryWarning={hasMemoryWarning(sysStatus.memoryNotices)}
                        systemWarning={hasMemoryWarning(sysStatus.systemNotices)} />

                    {/* New Chat ＋ 無痕對話（HAX G17，ADR-0086） */}
                    <div className="w-full px-2 flex flex-col gap-2">
                        <Button
                            variant="default"
                            onClick={() => {
                                handleNewSession();
                                if (activeMode !== 'chat') onChatClick?.();
                            }}
                            title={t('layout.left_sidebar.new_chat_button_tooltip')}
                            className="w-full h-12 px-3 rounded-xl bg-primary-button hover:bg-primary-button/90 text-white shadow-md shrink-0 focus:ring-2 focus:ring-primary/20"
                        >
                            <PlusCircle className="w-5 h-5 mr-2" />
                            <span className="font-bold">{t('layout.left_sidebar.new_chat_button')}</span>
                        </Button>
                        <Button
                            variant="outline"
                            onClick={() => {
                                handleNewSession(undefined, true);
                                if (activeMode !== 'chat') onChatClick?.();
                            }}
                            title={t('layout.left_sidebar.incognito_chat_button_tooltip')}
                            className="w-full h-9 px-3 rounded-xl border-border/60 bg-transparent text-muted-foreground hover:text-foreground hover:bg-accent/40 shadow-none shrink-0 focus:ring-2 focus:ring-primary/20"
                        >
                            <VenetianMask className="w-4 h-4 mr-2" />
                            <span className="font-bold text-sm">{t('layout.left_sidebar.incognito_chat_button')}</span>
                        </Button>
                    </div>

                    {/* Session List (SRP: SessionItem) */}
                    <div className="flex-grow overflow-y-auto custom-scrollbar flex flex-col gap-1 w-full px-2">
                        <div className="flex items-center justify-between px-2 mb-1">
                            <h2 className="text-xs font-black text-muted-foreground uppercase tracking-widest">{t('layout.left_sidebar.session_history_heading')}</h2>
                        </div>
                        
                        {mounted && Object.entries(sessionGroups).map(([groupName, groupSessions]) => (
                            <div key={groupName} className="flex flex-col gap-0.5">
                                <button 
                                    onClick={() => toggleGroup(groupName)}
                                    className="flex items-center gap-2 px-2 py-1.5 text-xs font-bold text-muted-foreground hover:text-primary transition-colors text-left"
                                >
                                    <span className={cn("transition-transform", collapsedGroups[groupName] ? "" : "rotate-90")}>▶</span>
                                    <span className="truncate">{groupName}</span>
                                    <span className="ml-auto bg-muted px-1.5 rounded-md text-xs">{groupSessions.length}</span>
                                </button>
                                
                                {!collapsedGroups[groupName] && groupSessions.map((s) => (
                                    <SessionItem 
                                        key={s.id}
                                        session={s}
                                        isActive={activeSessionId === s.id}
                                        isStreaming={streamingSessionIds.includes(s.id)}
                                        editingSessionId={sessionActions.editingSessionId}
                                        editTitle={sessionActions.editTitle}
                                        deleteConfirmId={sessionActions.deleteConfirmId}
                                        projects={projects}
                                        onSwitch={(id) => {
                                            handleSwitchSession(id);
                                            if (activeMode !== 'chat') onChatClick?.();
                                        }}
                                        onStartEdit={sessionActions.handleStartEdit}
                                        onSaveTitle={sessionActions.handleSaveTitle}
                                        onEditTitleChange={sessionActions.setEditTitle}
                                        onDelete={sessionActions.handleDelete}
                                        onClearDeleteConfirm={sessionActions.clearDeleteConfirm}
                                        onExport={sessionActions.handleExport}
                                        onSplitExport={sessionActions.handleSplitExport}
                                        onSetHome={handleSetHome}
                                        onUnsetHome={handleUnsetHome}
                                        onSetProject={handleSetProject}
                                    />
                                ))}
                            </div>
                        ))}
                    </div>

                    {/* Navigation (SRP/OCP: NavigationArea) */}
                    <NavigationArea 
                        activeMode={activeMode}
                        isHybrid={sysStatus.mode === 'hybrid'}
                        onChatClick={() => onChatClick?.()}
                        onProjectsClick={() => onProjectsClick?.()}
                        onSoulClick={() => onSoulClick?.()}
                        onStudioClick={() => onStudioClick?.()}
                        onPulseClick={() => onPulseClick?.()}
                        onVaultClick={() => onVaultClick?.()}
                        onSettingsClick={() => onSettingsClick?.()}
                    />
                </div>
            </aside>
        </div>
    );
}

export default React.memo(LeftSidebar);
