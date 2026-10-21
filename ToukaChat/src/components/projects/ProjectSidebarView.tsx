"use client";

import { useState, useEffect, useCallback } from 'react';
import { 
    Folder,
    FileText,
    ChevronRight,
    Users, 
    History,
    RefreshCw,
    HardDrive,
    ExternalLink,
    Activity,
    Loader2
} from 'lucide-react';
import { useTranslation } from "react-i18next";
import { useChat } from '@/lib/providers/ChatProvider';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { formatToTaipeiDate } from '@/lib/time_utils';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import WorkspaceSettings from './WorkspaceSettings';

import { useProjects, ProjectOverview, FileNode } from '@/hooks/useProjects';

export default function ProjectSidebarView() {
    const { t } = useTranslation();
    const { sessions: allSessions } = useSessionList();
    const { activeSessionId, handleSwitchSession, streamingSessionIds } = useChat();
    const { fetchProjectOverview, fetchProjectFiles } = useProjects();
    const [overview, setOverview] = useState<ProjectOverview | null>(null);
    const [fileTree, setFileTree] = useState<FileNode[]>([]);
    const [isLoading, setIsLoading] = useState(false);

    // Get current project_id from active session
    const activeSession = allSessions.find(s => s.id === activeSessionId);
    const projectId = activeSession?.project_id;
    // Sessions other than the active one (the empty list must trigger a fallback —
    // `[].map() || <p>` never fired because an empty array is truthy).
    const otherSessions = overview?.sessions.filter(s => s.id !== activeSessionId).slice(0, 5) ?? [];

    const fetchData = useCallback(async () => {
        if (!projectId) return;
        setIsLoading(true);
        
        try {
            const [ovData, treeData] = await Promise.all([
                fetchProjectOverview(projectId),
                fetchProjectFiles(projectId)
            ]);

            if (ovData) setOverview(ovData);
            setFileTree(treeData);
        } finally {
            setIsLoading(false);
        }
    }, [projectId, fetchProjectOverview, fetchProjectFiles]);


    useEffect(() => {
        if (projectId) {
            fetchData();
        } else {
            setOverview(null);
            setFileTree([]);
        }
    }, [projectId, fetchData]);

    return (
        <div className="flex flex-col h-full shrink-0">
            <div className="flex-1 overflow-y-auto p-4 custom-scrollbar">
                {!projectId ? (
                    <div className="flex flex-col items-center justify-center h-48 text-center opacity-60">
                        <Folder className="w-12 h-12 mb-4 text-muted-foreground" />
                        <p className="text-sm font-medium text-foreground">{t('projects.project_sidebar_view.no_project_linked')}</p>
                        <p className="text-xs mt-2 text-muted-foreground">{t('projects.project_sidebar_view.go_to_project_management')}</p>
                    </div>
                ) : (isLoading && !overview) ? (
                    <div className="flex items-center justify-center h-48">
                        <RefreshCw className="w-6 h-6 animate-spin text-primary" />
                    </div>
                ) : (
                    <div className="flex flex-col gap-6 animate-in fade-in duration-300">
                        {/* Header */}
                        <div className="flex flex-col gap-1.5">
                            <div className="flex items-center justify-between">
                                <h3 className="font-bold text-base text-foreground truncate">
                                    {overview?.project.name || t('projects.project_sidebar_view.loading')}
                                </h3>
                                <Button
                                    variant="ghost"
                                    size="icon"
                                    aria-label={t('projects.project_sidebar_view.refresh_project_data')}
                                    className="h-8 w-8 rounded-lg"
                                    onClick={fetchData}
                                    disabled={isLoading}
                                >
                                    <RefreshCw className={cn("w-4 h-4", isLoading && "animate-spin")} />
                                </Button>
                            </div>
                            <div className="flex items-center gap-1.5 text-xs text-muted-foreground font-mono bg-muted/50 p-1.5 rounded-md">
                                <HardDrive className="w-3 h-3 shrink-0" />
                                <span className="truncate">{overview?.project.root_path}</span>
                            </div>
                        </div>

                        {/* File Tree */}
                        <div className="flex flex-col gap-2">
                            <div className="items-center gap-2 mb-1 hidden md:flex">
                                <Folder className="w-4 h-4 text-primary" />
                                <span className="text-xs font-black uppercase tracking-widest text-muted-foreground">{t('projects.project_sidebar_view.file_tree_heading')}</span>
                            </div>
                            <div className="bg-background rounded-xl border border-border p-2 max-h-80 overflow-y-auto custom-scrollbar">
                                {fileTree.length > 0 ? (
                                    fileTree.map((node, i) => (
                                        <FileTreeNode key={i} node={node} depth={0} />
                                    ))
                                ) : (
                                    <p className="text-xs text-muted-foreground italic p-2 text-center">{t('projects.project_sidebar_view.empty_or_loading_directory')}</p>
                                )}
                            </div>
                        </div>

                        {/* Collaborators (Models) */}
                        <div className="flex flex-col gap-2">
                            <div className="flex items-center gap-2 mb-1">
                                <Users className="w-4 h-4 text-primary" />
                                <span className="text-xs font-black uppercase tracking-widest text-muted-foreground">{t('projects.project_sidebar_view.participating_agents')}</span>
                            </div>
                            <div className="flex flex-wrap gap-1.5">
                                {overview && overview.agents.length > 0 ? (
                                    overview.agents.map((agent, i) => (
                                        <div key={i} className="flex items-center gap-1.5 px-2 py-1 bg-primary/5 border border-primary/10 rounded-full text-xs font-bold text-primary">
                                            <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse"></span>
                                            {agent}
                                        </div>
                                    ))
                                ) : (
                                    <p className="text-xs text-muted-foreground italic">{t('projects.project_sidebar_view.no_info')}</p>
                                )}
                            </div>
                        </div>

                        {/* Related Sessions */}
                        <div className="flex flex-col gap-2">
                            <div className="flex items-center gap-2 mb-1">
                                <History className="w-4 h-4 text-primary" />
                                <span className="text-xs font-black uppercase tracking-widest text-muted-foreground">{t('projects.project_sidebar_view.related_sessions')}</span>
                            </div>
                            <div className="flex flex-col gap-1">
                                {otherSessions.length > 0 ? (
                                    otherSessions.map((s, i) => (
                                    <button
                                        key={i}
                                        onClick={() => handleSwitchSession(s.id)}
                                        className="flex items-center justify-between p-2 rounded-lg bg-muted/30 border border-transparent hover:border-primary/20 transition-all text-left group"
                                    >
                                        <div className="flex flex-col min-w-0 pr-2">
                                            <span className="flex items-center gap-1 text-xs font-semibold truncate group-hover:text-primary transition-colors">
                                                {streamingSessionIds.includes(s.id) && (
                                                    <Loader2 className="w-3 h-3 shrink-0 text-primary animate-spin" aria-label={t('projects.project_sidebar_view.generating_in_background')} />
                                                )}
                                                {s.title || t('projects.project_sidebar_view.untitled_session')}
                                            </span>
                                            <span className="text-xs text-muted-foreground">{formatToTaipeiDate(s.updatedAt)}</span>
                                        </div>
                                        <ExternalLink className="w-3 h-3 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity" />
                                    </button>
                                    ))
                                ) : (
                                    <p className="text-xs text-muted-foreground italic p-2 text-center">{t(
                                        overview && overview.sessions.length === 1
                                            ? 'projects.project_sidebar_view.only_session_in_project'
                                            : 'projects.project_sidebar_view.no_other_sessions'
                                    )}</p>
                                )}
                            </div>
                        </div>
                        
                        {/* Audit Log Hint */}
                        {overview?.recent_audits && overview.recent_audits.length > 0 && (
                            <div className="flex flex-col gap-2">
                                <div className="flex items-center gap-2 mb-1">
                                    <Activity className="w-4 h-4 text-primary" />
                                    <span className="text-xs font-black uppercase tracking-widest text-muted-foreground">{t('projects.project_sidebar_view.recent_file_changes')}</span>
                                </div>
                                <div className="space-y-2">
                                    {overview.recent_audits.map((audit, idx) => (
                                        <div key={idx} className="flex gap-2 text-xs border-l-2 border-primary/20 pl-2 py-0.5">
                                            <span className="font-bold text-primary shrink-0 uppercase">{audit.action}</span>
                                            <span className="text-muted-foreground truncate">{audit.summary}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>
            
            <div className="p-4 border-t border-border bg-sidebar/30">
                <WorkspaceSettings />
            </div>
        </div>
    );
}

function FileTreeNode({ node, depth }: { node: FileNode, depth: number }) {
    const [isOpen, setIsOpen] = useState(false);
    const hasChildren = node.children && node.children.length > 0;
    const isDir = node.type === 'directory';

    // a11y: a directory row is a real toggle button (keyboard-operable + aria-expanded);
    // a file row is non-interactive (static), so it is a plain div with no onClick.
    const rowClass = cn(
        "flex items-center gap-1.5 py-1 px-1.5 rounded-md transition-colors w-full text-left",
        isDir && "cursor-pointer hover:bg-muted",
        depth > 0 && "ml-3"
    );
    const rowInner = (
        <>
            {isDir ? (
                <>
                    <ChevronRight className={cn("w-3 h-3 text-muted-foreground transition-transform", isOpen && "rotate-90")} />
                    <Folder className="w-3.5 h-3.5 text-primary/80 shrink-0" />
                </>
            ) : (
                <>
                    <div className="w-3 h-3" /> {/* Spacer */}
                    <FileText className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                </>
            )}
            <span className="text-xs font-medium truncate text-foreground/80" title={node.path}>
                {node.name}
            </span>
        </>
    );

    return (
        <div className="flex flex-col min-w-0">
            {isDir ? (
                <button
                    type="button"
                    className={rowClass}
                    onClick={() => setIsOpen(!isOpen)}
                    aria-expanded={isOpen}
                >
                    {rowInner}
                </button>
            ) : (
                <div className={rowClass}>{rowInner}</div>
            )}
            {isDir && isOpen && hasChildren && (
                <div className="flex flex-col">
                    {node.children!.map((child, i) => (
                        <FileTreeNode key={i} node={child} depth={depth + 1} />
                    ))}
                </div>
            )}
        </div>
    );
}
