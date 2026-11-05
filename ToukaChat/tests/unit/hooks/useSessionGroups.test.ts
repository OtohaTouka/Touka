import { renderHook } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { useSessionGroups } from '@/hooks/useSessionGroups';
import { Session, Project } from '@/lib/types';

describe('useSessionGroups', () => {
  const mockSessions: Session[] = [
    { id: '1', title: 'Hello World', updatedAt: 123, messages: [], agent_id: 'ToukaSoulAlice' },
    { id: '2', title: 'Secret Project', updatedAt: 124, messages: [], project_id: 'p1', agent_id: 'ToukaGroup' },
    { id: '3', title: 'Normal Chat', updatedAt: 125, messages: [], agent_id: 'Bob' },
    { id: '4', title: 'Lost Project', updatedAt: 126, messages: [], project_id: 'gone' },
  ];

  const mockProjects: Project[] = [
    { id: 'p1', name: 'Alpha', root_path: '/a', description: 'desc', updatedAt: 123 },
  ];

  it('should group by projects', () => {
    const { result } = renderHook(() => useSessionGroups({ sessions: mockSessions, projects: mockProjects }));

    expect(result.current.sessionGroups).toHaveProperty('專案: Alpha');
    expect(result.current.sessionGroups).toHaveProperty('其他對話');
    expect(result.current.sessionGroups).toHaveProperty('未知專案');
    expect(result.current.sessionGroups['專案: Alpha']).toHaveLength(1);
    expect(result.current.sessionGroups['其他對話']).toHaveLength(2);
    expect(result.current.sessionGroups['未知專案']).toHaveLength(1);
  });
});
