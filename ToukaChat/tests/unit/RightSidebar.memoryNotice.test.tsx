import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import RightSidebar from '@/components/RightSidebar';

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: () => ({ activeTab: 'artifacts', setActiveTab: vi.fn(), artifacts: [] }),
}));
vi.mock('@/lib/SystemStatusContext', () => ({
  useSystemStatus: () => ({
    status: {
      systemDegraded: false,
      degradedReason: '',
      memoryNotices: [{ code: 'unmigrated_legacy_facts', level: 'info', count: 1 }],
    },
    checkStatus: vi.fn(),
  }),
}));
vi.mock('@/components/sidebar/SystemMonitoringView', () => ({ SystemMonitoringView: () => <div>System Monitor</div> }));
vi.mock('@/components/sidebar/DebugMonitoringView', () => ({ DebugMonitoringView: () => <div>Debug Monitor</div> }));
vi.mock('@/components/projects/ProjectSidebarView', () => ({ default: () => <div>Project Sidebar</div> }));
vi.mock('@/components/artifacts/MissionBoard', () => ({ default: () => <div>Mission Board</div> }));
vi.mock('@/components/artifacts/ArtifactView', () => ({ default: () => <div>Artifact View</div> }));

describe('RightSidebar memory notices', () => {
  it('shows memory notices across tabs without raising an alert', () => {
    render(<RightSidebar isOpen={true} onClose={vi.fn()} />);
    expect(screen.getByTestId('memory-notice')).toHaveTextContent(/舊記憶檔/);
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
