import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Session, Project } from '@/lib/types';

interface GroupedSessions {
  [groupName: string]: Session[];
}

interface UseSessionGroupsProps {
  sessions: Session[];
  projects: Project[];
}

/**
 * 專案畫面的對話分組（SOLID: SRP）。聊天畫面的靈魂資料夾在 `lib/session_tree.ts`。
 */
export function useSessionGroups({ sessions, projects }: UseSessionGroupsProps) {
  const { t } = useTranslation();

  const sessionGroups = useMemo(() => {
    return sessions.reduce((acc: GroupedSessions, sess) => {
      let groupName = t('hooks.use_session_groups.other_conversations');
      if (sess.project_id) {
        const proj = projects.find(p => p.id === sess.project_id);
        groupName = proj ? t('hooks.use_session_groups.project_group', { name: proj.name }) : t('hooks.use_session_groups.unknown_project');
      }
      if (!acc[groupName]) acc[groupName] = [];
      acc[groupName].push(sess);
      return acc;
    }, {});
  }, [sessions, projects, t]);

  return { sessionGroups };
}
