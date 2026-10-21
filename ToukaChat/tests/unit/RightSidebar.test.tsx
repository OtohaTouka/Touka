import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import RightSidebar from '@/components/RightSidebar';

// Mock useArtifacts
vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: () => ({
    activeTab: 'artifacts',
    setActiveTab: vi.fn(),
    artifacts: []
  })
}));

// Mock SystemStatusContext（RightSidebar 消費 useSystemStatus；此測試未包 Provider）
vi.mock('@/lib/SystemStatusContext', () => ({
  useSystemStatus: () => ({
    status: { systemDegraded: false, degradedReason: '' },
    checkStatus: vi.fn(),
  }),
}));

// Mock SystemMonitoringView
vi.mock('@/components/sidebar/SystemMonitoringView', () => ({
  SystemMonitoringView: () => <div>System Monitor</div>
}));

// Mock DebugMonitoringView
vi.mock('@/components/sidebar/DebugMonitoringView', () => ({
  DebugMonitoringView: () => <div>Debug Monitor</div>
}));

// Mock ProjectSidebarView
vi.mock('@/components/projects/ProjectSidebarView', () => ({
  default: () => <div>Project Sidebar</div>
}));

// Mock MissionBoard
vi.mock('@/components/artifacts/MissionBoard', () => ({
  default: () => <div>Mission Board</div>
}));

// Mock ArtifactView
vi.mock('@/components/artifacts/ArtifactView', () => ({
  default: () => <div>Artifact View</div>
}));

describe('RightSidebar Tabs Layout', () => {
  it('TDD-Red: renders RightSidebar and checks that tab layout handles titles appropriately without arbitrary style issues', () => {
    const { container } = render(<RightSidebar isOpen={true} onClose={vi.fn()} />);
    
    // 檢查五個 tab 是否存在
    const tabs = ['ARTIFACTS', 'MISSION', 'PROJECT', 'SYSTEM', 'DEBUG'];
    tabs.forEach(tab => {
      const button = screen.getByRole('button', { name: new RegExp(tab, 'i') });
      expect(button).toBeInTheDocument();
      // 斷言：為了防止嚴重截斷，不應該有過小的寬度限制，且樣式不應該含有 arbitrarily 指定的寬度
      expect(button.className).not.toContain('max-w-[');
    });
  });
});
