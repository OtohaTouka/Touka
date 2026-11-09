"use client";

import React from 'react';
import { useTranslation } from "react-i18next";
import {
    MoreVertical, Download, Pencil, Trash2, Home, Ban, Scissors, Folder
} from "lucide-react";
import { cn } from '@/lib/utils';
import { Session, Project } from '@/lib/types';
import type { SessionWorkStatus } from '@/lib/session_activity';
import { formatToTaipeiDate } from '@/lib/time_utils';
import { SessionStatusLead, SessionStatusTail, useBreathingClass, useStatusLabel } from './SessionStatusMark';
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

/** 這一列在對話清單（WAI-ARIA tree）裡的位置。 */
export interface SessionTreeRowProps {
  'data-tree-row': string;
  'aria-level': number;
  'aria-posinset': number;
  'aria-setsize': number;
  'aria-expanded'?: boolean;
  tabIndex: number;
}

interface SessionItemProps {
  session: Session;
  isActive: boolean;
  /** 工作狀態（ADR-0300）；資料夾那一列也算進收起來的子對話。 */
  status?: SessionWorkStatus | null;
  /** 列上的日期；工作中的對話用看到它在工作的時刻，預設是存檔時間。 */
  displayTime?: number;
  editingSessionId: string | null;
  editTitle: string;
  deleteConfirmId: string | null;
  projects: Project[];
  /** 只有認得出是靈魂的對話才給「設為家」，免得按了被拒絕。 */
  canSetHome?: boolean;
  /** 資料夾那一列：顯示靈魂名稱，對話標題放在 title。 */
  folderName?: string;
  /** 資料夾收合著，開著的對話在裡面：名稱要念出來，不然聽起來像家對話開著。 */
  holdsActive?: boolean;
  /** 名稱前面的東西（資料夾的箭頭）。 */
  leading?: React.ReactNode;
  /** 其他對話的列上標模型。 */
  modelLabel?: string;
  row?: SessionTreeRowProps;
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
  status,
  displayTime,
  editingSessionId,
  editTitle,
  deleteConfirmId,
  projects,
  canSetHome = false,
  folderName,
  holdsActive = false,
  leading,
  modelLabel,
  row,
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
  const [menuOpen, setMenuOpen] = React.useState(false);
  const rowRef = React.useRef<HTMLDivElement>(null);
  const descriptionId = React.useId();

  const isEditing = editingSessionId === session.id;
  const isConfirmingDelete = deleteConfirmId === session.id;

  // 改名結束，輸入框跟著消失：焦點回到這一列，不掉到頁面最前面
  const wasEditing = React.useRef(isEditing);
  React.useEffect(() => {
    if (wasEditing.current && !isEditing && document.activeElement === document.body) rowRef.current?.focus();
    wasEditing.current = isEditing;
  }, [isEditing]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // 輸入框裡是系統自己的選單
    if ((e.target as HTMLElement).tagName === 'INPUT') return;
    if (e.key === 'ContextMenu' || (e.key === 'F10' && e.shiftKey)) {
      e.preventDefault();
      setMenuOpen(true);
    }
  };

  const separator = t('sidebar.session_tree.label_separator');
  const statusLabel = useStatusLabel(status);
  const breathing = useBreathingClass(status);
  const shownTime = displayTime ?? session.updatedAt;
  const label = [
    folderName ? t('sidebar.session_tree.folder_home_aria', { name: folderName, title: session.title }) : session.title,
    holdsActive && t('sidebar.session_tree.folder_holds_active'),
    statusLabel,
    session.incognito && t('sidebar.session_item.incognito_label'),
    session.is_old_home && t('sidebar.session_item.old_home_badge'),
    modelLabel,
  ].filter(Boolean).join(separator);
  const roundCount = typeof session.round_count === 'number' && session.round_count > 0
    ? t('sidebar.session_item.round_count', { count: session.round_count })
    : null;
  // aria-label 蓋過列裡的文字，日期與則數另外給，報讀器才念得到
  const description = [formatToTaipeiDate(shownTime), roundCount].filter(Boolean).join(separator);

  return (
    // 整列是 treeitem、「⋯」在裡面但不在 Tab 順序（tabIndex=-1，Shift+F10 打開）。
    // treeitem 的子元素不會被當成展示用（不像 button），所以 axe 的 nested-interactive 不適用；
    // 一樣不能把列的角色寫成 button（A5 修過的問題）。
    <div
      ref={rowRef}
      // eslint-disable-next-line jsx-a11y/role-has-required-aria-props -- 這裡是導覽，目前對話用 aria-current；axe 不要求 aria-selected
      role="treeitem"
      aria-current={isActive ? 'true' : undefined}
      aria-label={label}
      aria-describedby={descriptionId}
      {...row}
      onKeyDown={handleKeyDown}
      onContextMenu={(e) => {
        // 鍵盤打開的（button 是 0）已經由上面接走，不要再跳瀏覽器選單；滑鼠右鍵照舊
        if (e.button === 0) e.preventDefault();
      }}
      className={cn(
        "group flex items-center rounded-xl transition-all",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
        (row?.['aria-level'] ?? 1) > 1 && "ml-5",
        isActive ? 'bg-accent shadow-sm' : 'hover:bg-accent/50'
      )}
    >
      <span id={descriptionId} hidden>{description}</span>
      {leading}
      {isEditing ? (
        <div className="flex flex-col min-w-0 flex-1 px-3 py-2.5">
          <input
            aria-label={t('sidebar.session_item.rename')}
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
        <div
          onClick={() => onSwitch(session.id)}
          onDoubleClick={(e) => onStartEdit(session, e)}
          title={folderName ? session.title : undefined}
          className={cn(
            "flex flex-col items-start min-w-0 flex-1 text-left rounded-xl cursor-pointer",
            folderName ? "px-1 py-1.5" : "px-3 py-2.5"
          )}
        >
          <div className="flex items-center gap-1.5 w-full">
            <SessionStatusLead status={status} />
            {folderName && <Home className="w-3.5 h-3.5 shrink-0 text-status-warning fill-status-warning" />}
            <span data-testid="session-name" className={cn("text-sm font-semibold truncate", breathing)}>{folderName ?? session.title}</span>
            {session.is_home && !folderName && <Home className="w-3.5 h-3.5 shrink-0 text-status-warning fill-status-warning" />}
            {session.is_old_home && (
              <span aria-hidden="true" className="shrink-0 rounded bg-muted px-1 text-xs font-bold text-muted-foreground">{t('sidebar.session_item.old_home_badge')}</span>
            )}
            {/* HAX G17（ADR-0086）：無痕對話標示。 */}
            {session.incognito && (
              <span aria-hidden="true" className="shrink-0 rounded bg-muted px-1 text-xs font-bold text-muted-foreground">{t('sidebar.session_item.incognito_badge')}</span>
            )}
            <SessionStatusTail status={status} />
            {modelLabel && <span className="ml-auto shrink-0 max-w-1/2 truncate text-xs text-muted-foreground">{modelLabel}</span>}
          </div>
          {!folderName && (
            <div className="flex items-center gap-2">
              <span suppressHydrationWarning className="text-xs text-muted-foreground">
                {formatToTaipeiDate(shownTime)}
              </span>
              {roundCount && (
                <span className="rounded-md bg-muted px-1.5 font-bold text-xs text-muted-foreground whitespace-nowrap">
                  {roundCount}
                </span>
              )}
            </div>
          )}
        </div>
      )}

      <div className="flex items-center pr-1 opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity" onClick={(e) => e.stopPropagation()}>
        <DropdownMenu open={menuOpen} onOpenChange={(open) => { setMenuOpen(open); if (!open) onClearDeleteConfirm(); }}>
          <DropdownMenuTrigger asChild>
            <button tabIndex={-1} aria-label={t('sidebar.session_item.session_options', { title: session.title })} className="p-1.5 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
              <MoreVertical className="w-4 h-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="z-50 w-40 rounded-xl border border-border bg-popover p-1 shadow-xl"
            onCloseAutoFocus={(e) => {
              // Radix 預設會把焦點給列裡的「⋯」，但「⋯」不在 Tab 順序，一律擋下
              e.preventDefault();
              const element = rowRef.current;
              if (!element) return;
              // 選單淡出跑完才走到這裡；這段時間焦點已經被移到別處（例如輸入框）就不搶回來，
              // 不然接著按的 Enter 會打在這一列上
              const active = document.activeElement;
              if (active && active !== document.body && !element.contains(active)) return;
              // 焦點回到這一列；選了重命名就留給輸入框
              (element.querySelector('input') ?? element).focus();
            }}
          >
            <DropdownMenuItem
              onClick={() => onExport(session)}
              className="flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors"
            >
              <Download className="w-4 h-4 text-muted-foreground" />
              {t('sidebar.session_item.export_conversation')}
            </DropdownMenuItem>
            {!session.is_old_home && <DropdownMenuItem
              onClick={() => onSplitExport(session)}
              className="flex items-center gap-2 px-2 py-2 text-sm font-medium rounded-lg cursor-pointer hover:bg-accent transition-colors"
            >
              <Scissors className="w-4 h-4 text-muted-foreground" />
              {t('sidebar.session_item.split_and_export')}
            </DropdownMenuItem>}
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

            {canSetHome && !session.is_home && (
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
