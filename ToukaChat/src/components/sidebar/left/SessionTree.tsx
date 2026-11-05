"use client";

import React, { useMemo } from 'react';
import { useTranslation } from "react-i18next";
import { ChevronRight, Folder, Home } from "lucide-react";
import { useAuth } from '@/contexts/AuthContext';
import { useSessionActions } from '@/hooks/useSessionActions';
import { useSessionGroups } from '@/hooks/useSessionGroups';
import { useSessionTreeKeyboard, type TreeRow } from '@/hooks/useSessionTreeKeyboard';
import { useSessionTreeState } from '@/hooks/useSessionTreeState';
import { useAgents } from '@/lib/providers/AgentProvider';
import { useChat } from '@/lib/providers/ChatProvider';
import { useProjects } from '@/lib/providers/ProjectProvider';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { buildSessionTree, canSetHome, previewChildren } from '@/lib/session_tree';
import type { Session } from '@/lib/types';
import { cn } from '@/lib/utils';
import { SessionItem, type SessionTreeRowProps } from './SessionItem';

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
  const { activeSessionId, handleSetHome, handleSetProject, handleUnsetHome, streamingSessionIds } = useChat();
  const { projects } = useProjects();
  const sessionActions = useSessionActions();
  const { isCollapsed, setCollapsed, isShowingAll, showAll } = useSessionTreeState(userId);
  const { sessionGroups } = useSessionGroups({ sessions, projects });

  const showingProjects = activeMode === 'projects';
  const waitingForSouls = !showingProjects && agentsStatus === 'loading';

  const { folderNodes, otherNodes } = useMemo(() => {
    const folderNodes: TreeNode[] = [];
    const otherNodes: TreeNode[] = [];

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
        folderNodes.push({ kind: 'folder', id, key, name, home: null, persist: false, isProject: true, expanded, level: 1, pos: index + 1, size: groups.length });
        if (expanded) addChildren(id, children);
      });
      return { folderNodes, otherNodes };
    }

    const tree = buildSessionTree(sessions, agents, agentsStatus);
    const pinned = new Set(activeSessionId ? [activeSessionId] : []);
    tree.folders.forEach((folder, index) => {
      const id = `folder:${folder.key}`;
      const expanded = !isCollapsed(folder.key);
      // 只有無痕對話的資料夾不寫進瀏覽器（ADR-0086）
      const persist = [folder.home, ...folder.children].some(session => session && !session.incognito);
      folderNodes.push({ kind: 'folder', id, key: folder.key, name: folder.name, home: folder.home, persist, isProject: false, expanded, level: 1, pos: index + 1, size: tree.folders.length });
      if (!expanded) return;
      const { visible, hiddenCount } = previewChildren(folder.children, isShowingAll(folder.key), pinned);
      addChildren(id, visible, hiddenCount > 0 ? { key: folder.key, count: hiddenCount } : undefined);
    });
    tree.others.forEach((session, index) => otherNodes.push({
      kind: 'session', id: session.id, parentId: null, session, level: 1, pos: index + 1, size: tree.others.length,
    }));
    return { folderNodes, otherNodes };
  }, [showingProjects, sessionGroups, sessions, agents, agentsStatus, activeSessionId, isCollapsed, isShowingAll]);

  const nodes = useMemo(() => [...folderNodes, ...otherNodes], [folderNodes, otherNodes]);

  const rows = useMemo<TreeRow[]>(() => nodes.map(node => ({
    id: node.id,
    parentId: node.kind === 'folder' ? null : node.parentId,
    ...(node.kind === 'folder' ? { expanded: node.expanded } : {}),
  })), [nodes]);

  const openFolder = (node: FolderNode) => {
    if (node.home) onSwitch(node.home.id);
    else setCollapsed(node.key, false, node.persist);
  };

  const toggle = (node: FolderNode, expanded: boolean | undefined) => setCollapsed(node.key, !!expanded, node.persist);

  const preferredId = nodes.find(node =>
    (node.kind === 'session' && node.session.id === activeSessionId) ||
    (node.kind === 'folder' && node.home?.id === activeSessionId))?.id ?? null;

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
      isStreaming={streamingSessionIds.includes(session.id)}
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
      onSetHome={handleSetHome}
      onUnsetHome={handleUnsetHome}
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
    const arrow = (
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
        leading: <>{arrow}<Home className="w-3.5 h-3.5 ml-1 shrink-0 text-status-warning fill-status-warning" /></>,
      });
    }
    return (
      <div
        key={node.id}
        // eslint-disable-next-line jsx-a11y/role-has-required-aria-props -- 這列不能選；axe 不要求 aria-selected
        role="treeitem"
        aria-label={node.name}
        {...rowProps(node)}
        onClick={() => openFolder(node)}
        className="flex items-center rounded-xl hover:bg-accent/50 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
      >
        {arrow}
        {!node.isProject && <Folder className="w-3.5 h-3.5 ml-1 shrink-0 text-muted-foreground" />}
        <span className="px-1 py-1.5 text-sm font-semibold truncate">{node.name}</span>
      </div>
    );
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
    </div>
  );
}
