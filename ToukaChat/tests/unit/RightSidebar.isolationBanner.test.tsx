import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import RightSidebar from '@/components/RightSidebar';

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: () => ({ activeTab: 'artifacts', setActiveTab: vi.fn(), artifacts: [] }),
}));
vi.mock('@/lib/SystemStatusContext', () => ({
  useSystemStatus: () => ({
    status: { systemDegraded: false, degradedReason: '', memoryNotices: [], systemNotices: [] },
    checkStatus: vi.fn(),
  }),
}));
vi.mock('@/hooks/useIsolationStatus', () => ({
  useIsolationStatus: () => ({
    level: 'none', expected: 'executor', reason: 'executor_unreachable', detail: null, features: [],
    local: { status: 'available', abi: 6, reason: null, net: null }, local_issue: null, executor: null,
    block_ports_issue: null, repairs: null, mcp: { unisolated: 0, waiting: 0, suspended: 0 },
  }),
}));
vi.mock('@/components/sidebar/SystemMonitoringView', () => ({ SystemMonitoringView: () => <div>System Monitor</div> }));
vi.mock('@/components/sidebar/DebugMonitoringView', () => ({ DebugMonitoringView: () => <div>Debug Monitor</div> }));
vi.mock('@/components/projects/ProjectSidebarView', () => ({ default: () => <div>Project Sidebar</div> }));
vi.mock('@/components/artifacts/MissionBoard', () => ({ default: () => <div>Mission Board</div> }));
vi.mock('@/components/artifacts/ArtifactView', () => ({ default: () => <div>Artifact View</div> }));

describe('RightSidebar isolation banner', () => {
  it('shows the isolation banner across tabs', () => {
    render(<RightSidebar isOpen={true} onClose={vi.fn()} />);
    expect(screen.getByTestId('isolation-banner')).toHaveTextContent('隔離比預期弱');
  });
});
