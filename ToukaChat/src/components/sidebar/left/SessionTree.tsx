"use client";

import React, { useMemo } from 'react';
import { useTranslation } from "react-i18next";
import { ChevronRight, Folder } from "lucide-react";
import { useAuth } from '@/contexts/AuthContext';
import { useSessionActions } from '@/hooks/useSessionActions';
import { useSessionGroups } from '@/hooks/useSessionGroups';
import { useSessionTreeKeyboard, type TreeRow } from '@/hooks/useSessionTreeKeyboard';
import { useSessionTreeState } from '@/hooks/useSessionTreeState';
import { useAgents } from '@/lib/providers/AgentProvider';
import { useChat } from '@/lib/providers/ChatProvider';
import { useProjects } from '@/lib/providers/ProjectProvider';
import { useSessionActivity } from '@/lib/providers/SessionActivityProvider';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { isBusyStatus, mostUrgentStatus, type SessionWorkStatus } from '@/lib/session_activity';
import { buildSessionTree, canSetHome, previewChildren, sessionSortTime } from '@/lib/session_tree';
import { useHomeConfirm } from '@/hooks/useHomeConfirm';
import { HomeConfirmDialog } from '@/components/chat/HomeConfirmDialog';
import type { Session } from '@/lib/types';
import { cn } from '@/lib/utils';
import { SessionItem, type SessionTreeRowProps } from './SessionItem';
import { SessionStatusLead, SessionStatusTail, useBreathingClass, useStatusLabel } from './SessionStatusMark';

const SKELETON_ROWS = [0, 1, 2];
const GROUP_CHAT_ID = 'ToukaGroup';

interface FolderNode {
  kind: 'folder';
  id: string;
  key: string;
  name: string;
  home: Session | null;
  persist: boolean;
  isProject: boolean;
  /** 收合著，開著的對話在裡面：這一列要標出來，不然畫面上找不到它。 */
  holdsActive: boolean;
  /** 畫面上看不到的子對話裡最要緊的狀態：收合了也要亮。 */
  hiddenStatus: SessionWorkStatus | null;
  /** 專案分組顯示筆數。 */
  count?: number;
}

interface SessionNode {
  kind: 'session';
  id: string;
  parentId: string | null;
  session: Session;
}

interface MoreNode {
  kind: 'more';
  id: string;
  parentId: string;
  key: string;
  count: number;
}

type TreeNode = (FolderNode | SessionNode | MoreNode) & { pos: number; size: number; level: number; expanded?: boolean };

interface SessionTreeProps {
  activeMode: string;
  onSwitch: (id: string) => void;
}

/** 沒有家對話的資料夾、專案分組。 */
function FolderRow({ node, arrow, rowProps, onOpen }: { node: FolderNode; arrow: React.ReactNode; rowProps: SessionTreeRowProps; onOpen: () => void }) {
  const { t } = useTranslation();
  const statusLabel = useStatusLabel(node.hiddenStatus);
  const breathing = useBreathingClass(node.hiddenStatus);
  return (
    <div
      // eslint-disable-next-line jsx-a11y/role-has-required-aria-props -- 這列不能選；axe 不要求 aria-selected
      role="treeitem"
      aria-current={node.holdsActive ? 'true' : undefined}
      aria-label={[node.name, node.count, node.holdsActive && t('sidebar.session_tree.folder_holds_active'), statusLabel].filter(Boolean).join(t('sidebar.session_tree.label_separator'))}
      {...rowProps}
      onClick={onOpen}
      className={cn(
        "flex items-center gap-1 rounded-xl cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
        node.holdsActive ? 'bg-accent shadow-sm' : 'hover:bg-accent/50'
      )}
    >
      {arrow}
      <SessionStatusLead status={node.hiddenStatus} />
      {!node.isProject && <Folder className="w-3.5 h-3.5 shrink-0 text-muted-foreground" />}
      <span data-testid="session-name" className={cn("pr-1 py-1.5 text-sm font-semibold truncate", breathing)}>{node.name}</span>
      <SessionStatusTail status={node.hiddenStatus} />
      {node.count !== undefined && <span className="ml-auto mr-2 rounded-md bg-muted px-1.5 text-xs text-muted-foreground">{node.count}</span>}
    </div>
  );
}

/** 平鋪的樹：每列帶 aria-level，資料夾與子對話是兄弟節點，事件不會互相冒泡。 */
export function SessionTree(props: SessionTreeProps) {
  const { user } = useAuth();
  // useLocalStorage 換鍵不重讀：換帳號就重掛，才不會留著上一個帳號的收合狀態
  return <SessionTreeBody key={user?.id ?? 'anonymous'} userId={user?.id} {...props} />;
}

function SessionTreeBody({ userId, activeMode, onSwitch }: SessionTreeProps & { userId?: string }) {
  const { t } = useTranslation();
  const { sessions } = useSessionList();
  const { agents, agentsStatus } = useAgents();
  const { activeSessionId, handleSetProject } = useChat();
  const home = useHomeConfirm();
  const { statusOf, activityTimeOf } = useSessionActivity();
  const { projects } = useProjects();
  const sessionActions = useSessionActions();
  const { isCollapsed, setCollapsed, isShowingAll, showAll } = useSessionTreeState(userId);
  const { sessionGroups } = useSessionGroups({ sessions, projects });

  const showingProjects = activeMode === 'projects';
  const waitingForSouls = !showingProjects && agentsStatus === 'loading';

  const { folderNodes, otherNodes } = useMemo(() => {
    const folderNodes: TreeNode[] = [];
    const otherNodes: TreeNode[] = [];

    const holdsActive = (children: Session[], expanded: boolean | undefined) =>
      expanded === false && children.some(session => session.id === activeSessionId);
    const statusOfHidden = (hidden: Session[]) => mostUrgentStatus(hidden.map(session => statusOf(session.id)));

    const addChildren = (parentId: string, children: Session[], more?: { key: string; count: number }) => {
      const size = children.length + (more ? 1 : 0);
      children.forEach((session, index) => folderNodes.push({
        kind: 'session', id: session.id, parentId, session, level: 2, pos: index + 1, size,
      }));
      if (more) folderNodes.push({ kind: 'more', id: `more:${more.key}`, parentId, ...more, level: 2, pos: size, size });
    };

    if (showingProjects) {
      const groups = Object.entries(sessionGroups);
      groups.forEach(([name, children], index) => {
        const key = `project:${name}`;
        const id = `folder:${key}`;
        const expanded = !isCollapsed(key);
        folderNodes.push({ kind: 'folder', id, key, name, home: null, persist: false, isProject: true, holdsActive: holdsActive(children, expanded), hiddenStatus: expanded ? null : statusOfHidden(children), count: children.length, expanded, level: 1, pos: index + 1, size: groups.length });
        if (expanded) addChildren(id, children);
      });
      return { folderNodes, otherNodes };
    }

    const tree = buildSessionTree(sessions, agents, agentsStatus, activityTimeOf);
    // 工作中、等您處理的不收進「還有 N 個」
    const pinned = new Set(sessions.filter(session => session.id === activeSessionId || isBusyStatus(statusOf(session.id))).map(session => session.id));
    tree.folders.forEach((folder, index) => {
      const id = `folder:${folder.key}`;
      // 只有家、底下沒有子對話：沒有東西可以收合
      const expanded = folder.children.length > 0 ? !isCollapsed(folder.key) : undefined;
      // 只有無痕對話的資料夾不寫進瀏覽器（ADR-0086）
      const persist = [folder.home, ...folder.children].some(session => session && !session.incognito);
      const { visible, hiddenCount } = previewChildren(folder.children, isShowingAll(folder.key), pinned);
      const hidden = expanded === false ? folder.children : folder.children.filter(session => !visible.includes(session));
      folderNodes.push({ kind: 'folder', id, key: folder.key, name: folder.name, home: folder.home, persist, isProject: false, holdsActive: holdsActive(folder.children, expanded), hiddenStatus: statusOfHidden(hidden), expanded, level: 1, pos: index + 1, size: tree.folders.length });
      if (!expanded) return;
      addChildren(id, visible, hiddenCount > 0 ? { key: folder.key, count: hiddenCount } : undefined);
    });
    tree.others.forEach((session, index) => otherNodes.push({
      kind: 'session', id: session.id, parentId: null, session, level: 1, pos: index + 1, size: tree.others.length,
    }));
    return { folderNodes, otherNodes };
  }, [showingProjects, sessionGroups, sessions, agents, agentsStatus, activeSessionId, isCollapsed, isShowingAll, statusOf, activityTimeOf]);

  const nodes = useMemo(() => [...folderNodes, ...otherNodes], [folderNodes, otherNodes]);

  const rows = useMemo<TreeRow[]>(() => nodes.map(node => ({
    id: node.id,
    parentId: node.kind === 'folder' ? null : node.parentId,
    ...(node.kind === 'folder' ? { expanded: node.expanded } : {}),
  })), [nodes]);

  const toggle = (node: FolderNode, expanded: boolean | undefined) => setCollapsed(node.key, !!expanded, node.persist);

  const openFolder = (node: FolderNode & { expanded?: boolean }) => {
    if (node.home) onSwitch(node.home.id);
    // 專案分組和改版前一樣，點名字也能收合
    else if (node.isProject) toggle(node, node.expanded);
    else setCollapsed(node.key, false, node.persist);
  };

  const preferredId = nodes.find(node =>
    (node.kind === 'session' && node.session.id === activeSessionId) ||
    (node.kind === 'folder' && (node.home?.id === activeSessionId || node.holdsActive)))?.id ?? null;

  const { rootRef, rootProps, currentId } = useSessionTreeKeyboard({
    rows,
    preferredId,
    onToggle: (row) => {
      const node = nodes.find(candidate => candidate.id === row.id);
      if (node?.kind === 'folder') toggle(node, row.expanded);
    },
    onActivate: (row) => {
      const node = nodes.find(candidate => candidate.id === row.id);
      if (node?.kind === 'session') onSwitch(node.session.id);
      else if (node?.kind === 'folder') openFolder(node);
      else if (node?.kind === 'more') showAll(node.key);
    },
  });

  if (waitingForSouls) {
    return (
      <div role="status" aria-busy="true" aria-label={t('sidebar.session_tree.loading')} className="flex flex-col gap-2 px-2">
        {SKELETON_ROWS.map(index => <div key={index} className="h-9 rounded-xl bg-muted animate-pulse" />)}
      </div>
    );
  }

  if (nodes.length === 0) return null;

  const rowProps = (node: TreeNode): SessionTreeRowProps => ({
    'data-tree-row': node.id,
    'aria-level': node.level,
    'aria-posinset': node.pos,
    'aria-setsize': node.size,
    ...(node.expanded === undefined ? {} : { 'aria-expanded': node.expanded }),
    tabIndex: node.id === currentId ? 0 : -1,
  });

  const renderSession = (session: Session, node: TreeNode, extra: Partial<React.ComponentProps<typeof SessionItem>> = {}) => (
    <SessionItem
      key={node.id}
      session={session}
      isActive={activeSessionId === session.id}
      status={statusOf(session.id)}
      displayTime={sessionSortTime(session, activityTimeOf)}
      editingSessionId={sessionActions.editingSessionId}
      editTitle={sessionActions.editTitle}
      deleteConfirmId={sessionActions.deleteConfirmId}
      projects={projects}
      canSetHome={canSetHome(session, agents)}
      row={rowProps(node)}
      onSwitch={onSwitch}
      onStartEdit={sessionActions.handleStartEdit}
      onSaveTitle={sessionActions.handleSaveTitle}
      onEditTitleChange={sessionActions.setEditTitle}
      onDelete={sessionActions.handleDelete}
      onClearDeleteConfirm={sessionActions.clearDeleteConfirm}
      onExport={sessionActions.handleExport}
      onSplitExport={sessionActions.handleSplitExport}
      onSetHome={home.askSetHome}
      onUnsetHome={home.askUnsetHome}
      onSetProject={handleSetProject}
      {...extra}
    />
  );

  const modelLabelOf = (agentId: string) => agentId === GROUP_CHAT_ID
    ? t('hooks.use_session_groups.group_chat')
    : agents.find(agent => agent.id === agentId)?.name ?? agentId;

  const renderNode = (node: TreeNode) => {
    if (node.kind === 'session') {
      const modelLabel = node.level === 1 && node.session.agent_id ? modelLabelOf(node.session.agent_id) : undefined;
      return renderSession(node.session, node, { modelLabel });
    }
    if (node.kind === 'more') {
      return (
        <div
          key={node.id}
          // eslint-disable-next-line jsx-a11y/role-has-required-aria-props -- 這列不能選；axe 不要求 aria-selected
          role="treeitem"
          {...rowProps(node)}
          onClick={() => showAll(node.key)}
          className="ml-5 px-3 py-1.5 rounded-xl text-xs font-bold text-muted-foreground hover:text-foreground hover:bg-accent/50 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
        >
          {t('sidebar.session_tree.more_sessions', { count: node.count })}
        </div>
      );
    }
    const arrow = node.expanded === undefined ? (
      // 沒有東西可以收合：留同樣的寬度，名稱才對齊
      <span aria-hidden="true" className="w-6 shrink-0" />
    ) : (
      // 只給滑鼠與觸控；鍵盤用左右鍵，不是另一個 Tab 站
      <span
        aria-hidden="true"
        data-testid="session-tree-toggle"
        onClick={(event) => { event.stopPropagation(); toggle(node, node.expanded); }}
        className="p-1.5 pr-0.5 shrink-0 cursor-pointer text-muted-foreground hover:text-foreground"
      >
        <ChevronRight className={cn("w-4 h-4 transition-transform", node.expanded && "rotate-90")} />
      </span>
    );
    if (node.home) {
      return renderSession(node.home, node, {
        folderName: node.name,
        leading: arrow,
        isActive: node.home.id === activeSessionId || node.holdsActive,
        holdsActive: node.holdsActive,
        status: mostUrgentStatus([statusOf(node.home.id), node.hiddenStatus]),
      });
    }
    return <FolderRow key={node.id} node={node} arrow={arrow} rowProps={rowProps(node)} onOpen={() => openFolder(node)} />;
  };

  return (
    <div ref={rootRef} role="tree" aria-label={t('layout.left_sidebar.session_history_heading')} className="flex flex-col gap-0.5" {...rootProps}>
      {folderNodes.map(renderNode)}
      {otherNodes.length > 0 && (
        <div role="group" aria-label={t('hooks.use_session_groups.other_conversations')} className="flex flex-col gap-0.5 mt-2">
          <div aria-hidden="true" className="px-2 py-1 text-xs font-bold text-muted-foreground">{t('hooks.use_session_groups.other_conversations')}</div>
          {otherNodes.map(renderNode)}
        </div>
      )}
      <HomeConfirmDialog request={home.request} onCancel={home.cancel} onConfirm={home.confirm} />
    </div>
  );
}
