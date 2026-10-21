import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Session, Project } from '@/lib/types';

interface GroupedSessions {
  [groupName: string]: Session[];
}

interface UseSessionGroupsProps {
  sessions: Session[];
  projects: Project[];
  activeMode: string;
  searchQuery: string;
}

/**
 * Hook for session filtering and grouping logic (SOLID: SRP)
 */
export function useSessionGroups({
  sessions,
  projects,
  activeMode,
  searchQuery
}: UseSessionGroupsProps) {
  const { t } = useTranslation();

  const filteredSessions = useMemo(() => {
    return sessions.filter(s =>
      s.title.toLowerCase().includes(searchQuery.toLowerCase())
    );
  }, [sessions, searchQuery]);

  const sessionGroups = useMemo(() => {
    return filteredSessions.reduce((acc: GroupedSessions, sess) => {
      let groupName = t('hooks.use_session_groups.unassigned_agent');

      if (activeMode === 'projects') {
        if (sess.project_id) {
          const proj = projects.find(p => p.id === sess.project_id);
          groupName = proj ? t('hooks.use_session_groups.project_group', { name: proj.name }) : t('hooks.use_session_groups.unknown_project');
        } else {
          groupName = t('hooks.use_session_groups.other_conversations');
        }
      } else {
        groupName = sess.agent_id || t('hooks.use_session_groups.unassigned_agent');
        // 簡化顯示名稱：移除 ToukaSoul 或 ToukaGroup 前綴
        if (groupName.startsWith("ToukaSoul")) {
          groupName = groupName.replace("ToukaSoul", "");
        } else if (groupName === "ToukaGroup") {
          groupName = t('hooks.use_session_groups.group_chat');
        }
      }

      if (!acc[groupName]) acc[groupName] = [];
      acc[groupName].push(sess);
      return acc;
    }, {});
  }, [filteredSessions, activeMode, projects, t]);

  return {
    filteredSessions,
    sessionGroups
  };
}
