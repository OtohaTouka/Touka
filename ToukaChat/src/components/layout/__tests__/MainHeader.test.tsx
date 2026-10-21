import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { MainHeader } from '../MainHeader';

const mockProps = {
  viewMode: "chat" as const,
  setViewMode: vi.fn(),
  isIDE: false,
  isMobile: false,
  isSidebarCollapsed: false,
  onToggleSidebar: vi.fn(),
  isRightSidebarOpen: false,
  onToggleRightSidebar: vi.fn(),
  sysStatus: { mode: 'hybrid', status: 'online' }
};

describe('MainHeader', () => {
  it('renders correctly in web mode', () => {
    render(<MainHeader {...mockProps} />);
    expect(screen.getAllByText(/聊天大廳/i)).not.toHaveLength(0);
    expect(screen.getAllByText(/Touka/i)).not.toHaveLength(0);
  });

  it('hides full labels in IDE mode', () => {
    render(<MainHeader {...mockProps} isIDE={true} />);
    // In IDE mode, the nav buttons and h1 labels are hidden or simplified
    const labels = screen.queryByText(/聊天大廳/i);
    // Note: getByText might find it if it's just hidden via CSS, 
    // but in my implementation, !isIDE guards the middle section.
    expect(labels).toBeNull();
  });

  it('renders system vault when in hybrid mode', () => {
    render(<MainHeader {...mockProps} />);
    expect(screen.getByText(/記憶寶庫/i)).toBeDefined();
  });
});
