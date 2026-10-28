// src/components/LeftSidebar.tsx
"use client";

import React, { useState, useEffect, useRef } from 'react';
import { useTranslation } from "react-i18next";
import { Button } from '@/components/ui/button';
import { useSystemStatus } from '@/lib/SystemStatusContext';
import { hasMemoryWarning } from '@/lib/memoryNotices';
import { cn } from '@/lib/utils';
import { useChat } from '@/lib/providers/ChatProvider';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { useProjects } from '@/lib/providers/ProjectProvider';
import { PanelLeftClose, PlusCircle, VenetianMask } from "lucide-react";

// Hooks
import { useSessionGroups } from '@/hooks/useSessionGroups';
import { useSessionActions } from '@/hooks/useSessionActions';

// Components
import { SidebarIdentity } from './sidebar/left/SidebarIdentity';
import { SidebarSetupNotices } from './sidebar/left/SidebarSetupNotices';
import { NavigationArea } from './sidebar/left/NavigationArea';
import { SessionItem } from './sidebar/left/SessionItem';

/** 展開後交焦點最多試幾個畫面（60Hz 約 0.5 秒，比 300ms 的過場長）。 */
const EXPAND_FOCUS_MAX_FRAMES = 30;

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
    /** 按下首次設定警告的按鈕後呼叫（手機版用來關掉選單，讓精靈不被擋住）。 */
    onSetupAction?: () => void;
    /** 桌面版才傳：在頂端畫收合鈕（手機版的 Sheet 有自己的關閉）。 */
    onCollapse?: () => void;
}

function LeftSidebar({ 
    onSoulClick, onStudioClick, onChatClick, onPulseClick, onVaultClick, onSettingsClick, onProjectsClick, onSetupAction, onCollapse,
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

    // WCAG 2.4.3：側欄剛展開、焦點跟著主區的展開鈕消失（掉到 body）時，交給收合鈕。
    // 外層的 visibility 有 300ms 過場：過場剛開始的那一刻瀏覽器仍當它看不見、focus() 會失效，
    // 所以每個畫面試一次，交到為止；焦點已被移到別處，或試到過場早該結束時就停。
    const collapseRef = useRef<HTMLButtonElement>(null);
    const wasCollapsed = useRef(isCollapsed);
    useEffect(() => {
        const justExpanded = !isCollapsed && wasCollapsed.current;
        wasCollapsed.current = isCollapsed;
        if (!justExpanded) return;
        let frame = 0;
        let tries = 0;
        const tryFocus = () => {
            const active = document.activeElement;
            if (active && active !== document.body) return;
            collapseRef.current?.focus();
            tries += 1;
            if (document.activeElement !== collapseRef.current && tries < EXPAND_FOCUS_MAX_FRAMES) {
                frame = requestAnimationFrame(tryFocus);
            }
        };
        frame = requestAnimationFrame(tryFocus);
        return () => cancelAnimationFrame(frame);
    }, [isCollapsed]);

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
                {/* 側欄太矮（例如首次設定警告佔了位置）時整欄捲動，底部導覽與登出一定點得到 */}
                <div className="py-4 flex flex-col gap-6 h-full relative transition-all duration-300 w-full min-w-0 px-4 overflow-y-auto overflow-x-hidden custom-scrollbar">
                    
                    {/* Identity (SRP: SidebarIdentity)＋桌面版收合鈕；側欄拖窄到擺不下時，收合鈕換到下一行靠右，不蓋住身分與警告標記 */}
                    <div className="flex flex-wrap items-center gap-1 w-full min-w-0">
                        <div className="flex-1">
                            <SidebarIdentity mode={sysStatus.mode} checkState={sysStatus.checkState}
                                memoryWarning={hasMemoryWarning(sysStatus.memoryNotices)}
                                systemWarning={hasMemoryWarning(sysStatus.systemNotices)} />
                        </div>
                        {onCollapse && (
                            <button
                                ref={collapseRef}
                                type="button"
                                onClick={onCollapse}
                                aria-label={t('layout.left_sidebar.collapse_sidebar')}
                                title={t('layout.left_sidebar.collapse_sidebar')}
                                className="ml-auto p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent/40 transition-colors shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                            >
                                <PanelLeftClose size={18} />
                            </button>
                        )}
                    </div>

                    {/* 首次設定警告（管理員密碼還是臨時的、初次設定還沒完成；ADR-0053） */}
                    <SidebarSetupNotices onAction={onSetupAction} />

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
                    <div className="flex-grow min-h-12 overflow-y-auto custom-scrollbar flex flex-col gap-1 w-full px-2">
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
