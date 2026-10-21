import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import React from 'react';
import { SessionItem } from '@/components/sidebar/left/SessionItem';
import { Session } from '@/lib/types';

interface MockProps {
  children?: React.ReactNode;
}

interface MockMenuProps extends MockProps {
  onOpenChange?: (open: boolean) => void;
}

interface MockItemProps extends MockProps {
  onSelect?: (e: Event) => void;
}

// Mock dropdown menu to trigger onOpenChange(false)
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children, onOpenChange }: MockMenuProps) => {
    React.useEffect(() => {
      if (onOpenChange) {
        onOpenChange(false);
      }
    }, [onOpenChange]);
    return <div>{children}</div>;
  },
  DropdownMenuTrigger: ({ children }: MockProps) => <div>{children}</div>,
  DropdownMenuContent: ({ children }: MockProps) => <div>{children}</div>,
  DropdownMenuItem: ({ children, onSelect }: MockItemProps) => (
    <div onClick={(e) => onSelect?.(e.nativeEvent)}>{children}</div>
  ),
  DropdownMenuSub: ({ children }: MockProps) => <div>{children}</div>,
  DropdownMenuSubTrigger: ({ children }: MockProps) => <div>{children}</div>,
  DropdownMenuSubContent: ({ children }: MockProps) => <div>{children}</div>,
  DropdownMenuPortal: ({ children }: MockProps) => <div>{children}</div>,
}));

describe('SessionItem Component (TDD-Red)', () => {
  const mockSession: Session = {
    id: 'session_123',
    title: 'Test Session',
    is_home: false,
    updatedAt: 1234567890,
    round_count: 5,
    messages: []
  };

  const defaultProps = {
    session: mockSession,
    isActive: true,
    editingSessionId: null,
    editTitle: '',
    deleteConfirmId: null,
    projects: [],
    onSwitch: vi.fn(),
    onStartEdit: vi.fn(),
    onSaveTitle: vi.fn(), 
    onEditTitleChange: vi.fn(),
    onDelete: vi.fn(), 
    onExport: vi.fn(),
    onSplitExport: vi.fn(),
    onSetHome: vi.fn(),
    onUnsetHome: vi.fn(),
    onSetProject: vi.fn(),
    onClearDeleteConfirm: vi.fn()
  };

  it('TDD-Red: compiles with new contract (no Event arguments in onSaveTitle and onDelete)', () => {
    const onDeleteMock = vi.fn();
    const onSaveTitleMock = vi.fn();

    render(
      <SessionItem 
        {...defaultProps} 
        onDelete={onDeleteMock} 
        onSaveTitle={onSaveTitleMock} 
      />
    );
    expect(true).toBe(true);
  });

  it('TDD-Red: triggers onClearDeleteConfirm callback when dropmenu closes', () => {
    const onClearDeleteConfirmMock = vi.fn();

    render(
      <SessionItem 
        {...defaultProps} 
        onClearDeleteConfirm={onClearDeleteConfirmMock} 
      />
    );

    expect(onClearDeleteConfirmMock).toHaveBeenCalled();
  });
});
