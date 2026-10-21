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
      memoryNotices: [],
      systemNotices: [{ code: 'autonomy_config_unreadable', level: 'warning' }],
    },
    checkStatus: vi.fn(),
  }),
}));
vi.mock('@/components/sidebar/SystemMonitoringView', () => ({ SystemMonitoringView: () => <div>System Monitor</div> }));
vi.mock('@/components/sidebar/DebugMonitoringView', () => ({ DebugMonitoringView: () => <div>Debug Monitor</div> }));
vi.mock('@/components/projects/ProjectSidebarView', () => ({ default: () => <div>Project Sidebar</div> }));
vi.mock('@/components/artifacts/MissionBoard', () => ({ default: () => <div>Mission Board</div> }));
vi.mock('@/components/artifacts/ArtifactView', () => ({ default: () => <div>Artifact View</div> }));

describe('RightSidebar system notices', () => {
  it('shows a system notice as an alert across tabs', () => {
    render(<RightSidebar isOpen={true} onClose={vi.fn()} />);
    const notice = screen.getByTestId('system-notice');
    expect(notice).toHaveAttribute('role', 'alert');
    expect(notice).toHaveTextContent(/autonomy\.json/);
  });
});
