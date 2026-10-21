"use client";

import React from 'react';
import { useTranslation } from "react-i18next";
import {
    MoreVertical, Download, Pencil, Trash2, Home, Ban, Scissors, Folder, Loader2
} from "lucide-react";
import { cn } from '@/lib/utils';
import { Session, Project } from '@/lib/types';
import { formatToTaipeiDate } from '@/lib/time_utils';
import { 
    DropdownMenu, 
    DropdownMenuContent, 
    DropdownMenuItem, 
    DropdownMenuTrigger,
    DropdownMenuSub,
    DropdownMenuSubTrigger,
    DropdownMenuSubContent,
    DropdownMenuPortal
} from "@/components/ui/dropdown-menu";

interface SessionItemProps {
  session: Session;
  isActive: boolean;
  /** ADR-0080：此對話有進行中（含背景）串流時顯示生成指示。 */
  isStreaming?: boolean;
  editingSessionId: string | null;
  editTitle: string;
  deleteConfirmId: string | null;
  projects: Project[];
  onSwitch: (id: string) => void;
  onStartEdit: (session: Session, e?: React.MouseEvent) => void;
  onSaveTitle: (id: string) => void;
  onEditTitleChange: (val: string) => void;
  onDelete: (id: string) => void;
  onClearDeleteConfirm: () => void;
  onExport: (session: Session, e?: React.MouseEvent) => void;
  onSplitExport: (session: Session, e?: React.MouseEvent) => void;
  onSetHome: (id: string) => void;
  onUnsetHome: (id: string) => void;
  onSetProject: (sessionId: string, projectId: string | null) => void;
}

export function SessionItem({
  session,
  isActive,
  isStreaming = false,
  editingSessionId,
  editTitle,
  deleteConfirmId,
  projects,
  onSwitch,
  onStartEdit,
  onSaveTitle,
  onEditTitleChange,
  onDelete,
  onClearDeleteConfirm,
  onExport,
  onSplitExport,
  onSetHome,
  onUnsetHome,
  onSetProject
}: SessionItemProps) {
  const { t } = useTranslation();

  const isEditing = editingSessionId === session.id;
  const isConfirmingDelete = deleteConfirmId === session.id;

  return (
    // WCAG 2.1.1＋4.1.2（A2→A5 axe 修正）：外層為純容器（不再帶 button 角色——
    // 避免 nested-interactive：互動元素內含互動元素）；「切換會話」由標題區的
    // 真 <button> 承擔（原生鍵盤語意），選單觸發鈕為兄弟節點並具名。
    <div
      className={cn(
        "group flex items-center rounded-xl transition-all ml-2",
        isActive ? 'bg-accent shadow-sm' : 'hover:bg-accent/50'
      )}
    >
      {isEditing ? (
        <div className="flex flex-col min-w-0 flex-1 px-3 py-2.5">
          <input
            value={editTitle}
            onChange={e => onEditTitleChange(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && onSaveTitle(session.id)}
            onBlur={() => onSaveTitle(session.id)}
            autoFocus
            className="bg-muted text-sm font-semibold rounded px-1 outline-none focus-visible:ring-2 focus-visible:ring-primary w-full"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      ) : (
        <button
          type="button"
          onClick={() => onSwitch(session.id)}
          onDoubleClick={(e) => onStartEdit(session, e)}
          aria-current={isActive ? 'true' : undefined}
          className={cn(
            "flex flex-col items-start min-w-0 flex-1 px-3 py-2.5 text-left rounded-xl cursor-pointer",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          )}
        >
          <div className="flex items-center gap-1.5 w-full">
            {isStreaming && (
              <Loader2 className="w-3.5 h-3.5 shrink-0 text-primary animate-spin" aria-label={t('sidebar.session_item.background_generating')} />
            )}
            <span className="text-sm font-semibold truncate">{session.title}</span>
            {session.is_home && <Home className="w-3.5 h-3.5 text-status-warning fill-status-warning" />}
            {/* HAX G17（ADR-0086）：無痕對話標示。 */}
            {session.incognito && (
              <span aria-label={t('sidebar.session_item.incognito_label')} className="shrink-0 rounded bg-muted px-1 text-xs font-bold text-muted-foreground">{t('sidebar.session_item.incognito_badge')}</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <span suppressHydrationWarning className="text-xs text-muted-foreground">
              {formatToTaipeiDate(session.updatedAt)}
            </span>
            {typeof session.round_count === 'number' && session.round_count > 0 && (
              <span className="rounded-md bg-muted px-1.5 font-bold text-xs text-muted-foreground">
                {t('sidebar.session_item.round_count', { count: session.round_count })}
              </span>
            )}
          </div>
        </button>
      )}

      <div className="flex items-center pr-1 opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus-within:opacity-100 transition-opacity" onClick={(e) => e.stopPropagation()}>
        <DropdownMenu onOpenChange={(open) => { if (!open) onClearDeleteConfirm(); }}>
          <DropdownMenuTrigger asChild>
            <button aria-label={t('sidebar.session_item.session_options', { title: session.title })} className="p-1.5 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
              <MoreVertical className="w-4 h-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="z-50 w-40 rounded-xl border border-border bg-popover p-1 shadow-xl">
            <DropdownMenuItem 
              onClick={() => onExport(session)}
              className="flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors"
            >
              <Download className="w-4 h-4 text-muted-foreground" />
              {t('sidebar.session_item.export_conversation')}
            </DropdownMenuItem>
            <DropdownMenuItem 
              onClick={() => onSplitExport(session)}
              className="flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors"
            >
              <Scissors className="w-4 h-4 text-muted-foreground" />
              {t('sidebar.session_item.split_and_export')}
            </DropdownMenuItem>
            <DropdownMenuItem 
              onClick={() => onStartEdit(session)}
              className="flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors"
            >
              <Pencil className="w-4 h-4 text-muted-foreground" />
              {t('sidebar.session_item.rename')}
            </DropdownMenuItem>
            <DropdownMenuSub>
              <DropdownMenuSubTrigger className="flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors">
                <Folder className="w-4 h-4 text-muted-foreground" />
                {t('sidebar.session_item.manage_project_association')}
              </DropdownMenuSubTrigger>
              <DropdownMenuPortal>
                <DropdownMenuSubContent className="z-50 w-48 rounded-xl border border-border bg-popover p-1 shadow-xl">
                  <DropdownMenuItem 
                    onClick={() => onSetProject(session.id, null)}
                    className="flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors"
                  >
                    <Ban className="w-4 h-4 text-muted-foreground" />
                    {t('sidebar.session_item.no_project')}
                  </DropdownMenuItem>
                  <div className="h-px bg-border my-1" />
                  {projects.map(p => (
                    <DropdownMenuItem 
                      key={p.id}
                      onClick={() => onSetProject(session.id, p.id)}
                      className={cn(
                        "flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors",
                        session.project_id === p.id && "bg-primary/10 text-primary"
                      )}
                    >
                      <Folder className="w-4 h-4" />
                      <span className="truncate">{p.name}</span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuSubContent>
              </DropdownMenuPortal>
            </DropdownMenuSub>

            {!session.is_home && (
              <DropdownMenuItem 
                onClick={() => onSetHome(session.id)}
                className="flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors text-status-warning"
              >
                <Home className="w-4 h-4" />
                {t('sidebar.session_item.set_as_home')}
              </DropdownMenuItem>
            )}
            {session.is_home && (
              <DropdownMenuItem 
                onClick={() => onUnsetHome(session.id)}
                className="flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors text-muted-foreground"
              >
                <Ban className="w-4 h-4" />
                {t('sidebar.session_item.unset_home')}
              </DropdownMenuItem>
            )}
            <DropdownMenuItem 
              onSelect={(e) => {
                // 等非同步結果；Radix 自動關閉會先清掉失敗確認。主動關閉仍重設。
                e.preventDefault();
                onDelete(session.id);
              }}
              className={cn(
                "flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer transition-colors",
                isConfirmingDelete 
                  ? "bg-status-error text-white hover:opacity-90" 
                  : "text-status-error hover:bg-status-error/10"
              )}
            >
              <Trash2 className={cn("w-4 h-4", isConfirmingDelete ? "text-white" : "text-status-error/80")} />
              {/* HAX G16：確認態標示永久性（不可復原）。 */}
              {isConfirmingDelete ? t('sidebar.session_item.confirm_delete') : t('sidebar.session_item.delete_conversation')}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
