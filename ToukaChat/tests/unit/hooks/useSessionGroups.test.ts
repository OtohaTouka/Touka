import { renderHook } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { useSessionGroups } from '@/hooks/useSessionGroups';
import { Session, Project } from '@/lib/types';

describe('useSessionGroups', () => {
  const mockSessions: Session[] = [
    { id: '1', title: 'Hello World', updatedAt: 123, messages: [], agent_id: 'ToukaSoulAlice' },
    { id: '2', title: 'Secret Project', updatedAt: 124, messages: [], project_id: 'p1', agent_id: 'ToukaGroup' },
    { id: '3', title: 'Normal Chat', updatedAt: 125, messages: [], agent_id: 'Bob' },
  ];

  const mockProjects: Project[] = [
    { id: 'p1', name: 'Alpha', root_path: '/a', description: 'desc', updatedAt: 123 },
  ];

  it('should filter sessions by searchQuery', () => {
    const { result } = renderHook(() => useSessionGroups({
      sessions: mockSessions,
      projects: mockProjects,
      activeMode: 'chat',
      searchQuery: 'secret'
    }));

    expect(result.current.filteredSessions).toHaveLength(1);
    expect(result.current.filteredSessions[0].id).toBe('2');
  });

  it('should group by agent_id and simplify names in chat mode', () => {
    const { result } = renderHook(() => useSessionGroups({
      sessions: mockSessions,
      projects: mockProjects,
      activeMode: 'chat',
      searchQuery: ''
    }));

    // ToukaSoulAlice -> Alice
    // ToukaGroup -> 群聊
    // Bob -> Bob
    expect(result.current.sessionGroups).toHaveProperty('Alice');
    expect(result.current.sessionGroups).toHaveProperty('群聊');
    expect(result.current.sessionGroups).toHaveProperty('Bob');
    expect(result.current.sessionGroups['Alice']).toHaveLength(1);
  });

  it('should group by projects when activeMode is projects', () => {
    const { result } = renderHook(() => useSessionGroups({
      sessions: mockSessions,
      projects: mockProjects,
      activeMode: 'projects',
      searchQuery: ''
    }));

    expect(result.current.sessionGroups).toHaveProperty('專案: Alpha');
    expect(result.current.sessionGroups).toHaveProperty('其他對話');
    expect(result.current.sessionGroups['專案: Alpha']).toHaveLength(1);
    expect(result.current.sessionGroups['其他對話']).toHaveLength(2);
  });
});
