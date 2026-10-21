import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, createEvent } from '@testing-library/react';
import { SessionItem } from '@/components/sidebar/left/SessionItem';
import React from 'react';

// Mock components that might interfere with testing
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuItem: ({ children, onSelect, className }: any) => (
    <div 
      data-testid="dropdown-item" 
      className={className}
      onClick={(e) => {
        if (onSelect) onSelect(e);
      }}
    >
      {children}
    </div>
  ),
  DropdownMenuSub: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSubTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSubContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuPortal: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

describe('SessionItem Deletion Logic', () => {
  const mockSession = {
    id: 's1',
    title: 'Test Session',
    updatedAt: new Date().toISOString(),
    messages: [],
    is_home: false,
  } as any;

  const defaultProps = {
    session: mockSession,
    isActive: false,
    editingSessionId: null,
    editTitle: '',
    deleteConfirmId: null,
    projects: [],
    onSwitch: vi.fn(),
    onStartEdit: vi.fn(),
    onSaveTitle: vi.fn(),
    onEditTitleChange: vi.fn(),
    onDelete: vi.fn(),
    onClearDeleteConfirm: vi.fn(),
    onExport: vi.fn(),
    onSplitExport: vi.fn(),
    onSetHome: vi.fn(),
    onUnsetHome: vi.fn(),
    onSetProject: vi.fn(),
  };

  it('should call preventDefault on the first delete click to keep menu open', () => {
    const onDeleteMock = vi.fn();
    render(<SessionItem {...defaultProps} onDelete={onDeleteMock} />);
    
    const deleteItem = screen.getByText('刪除對話');
    
    const myEvent = createEvent.click(deleteItem);
    const preventDefaultSpy = vi.spyOn(myEvent, 'preventDefault');
    
    fireEvent(deleteItem, myEvent);
    
    expect(onDeleteMock).toHaveBeenCalled();
    expect(preventDefaultSpy).toHaveBeenCalled();
  });

  it('keeps the menu open on confirmation until the asynchronous delete result is known', () => {
    const onDeleteMock = vi.fn();
    const propsWithConfirm = {
      ...defaultProps,
      deleteConfirmId: 's1',
      onDelete: onDeleteMock,
    };
    
    render(<SessionItem {...propsWithConfirm} />);

    // HAX G16：確認態文字加註不可復原（後果揭示）
    const deleteItem = screen.getByText('確認刪除？不可復原');
    
    const myEvent = createEvent.click(deleteItem);
    const preventDefaultSpy = vi.spyOn(myEvent, 'preventDefault');
    
    fireEvent(deleteItem, myEvent);
    
    expect(onDeleteMock).toHaveBeenCalled();
    expect(preventDefaultSpy).toHaveBeenCalled();
  });
});
