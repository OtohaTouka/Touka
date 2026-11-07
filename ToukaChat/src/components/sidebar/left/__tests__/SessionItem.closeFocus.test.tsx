/**
 * 「⋯」選單關掉後焦點回到哪裡。
 *
 * 選單有淡出動畫，關掉的那一刻和焦點回來之間隔了一段時間。這段時間裡焦點已經被移到別處
 * （例如輸入框）時，不能再拉回這一列：不然接著按的 Enter 會打在側欄上，等於點了這段對話。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SessionItem } from '@/components/sidebar/left/SessionItem';
import type { Session } from '@/lib/types';

type CloseAutoFocus = (event: Event) => void;
const captured: { onCloseAutoFocus?: CloseAutoFocus } = {};

vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuContent: ({ children, onCloseAutoFocus }: { children: React.ReactNode; onCloseAutoFocus?: CloseAutoFocus }) => {
    captured.onCloseAutoFocus = onCloseAutoFocus;
    return <div>{children}</div>;
  },
  DropdownMenuItem: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSub: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSubTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSubContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuPortal: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const session = { id: 's1', title: '這一段', updatedAt: 1, messages: [], is_home: false } as unknown as Session;

const props = {
  session,
  isActive: true,
  editingSessionId: null,
  editTitle: '',
  deleteConfirmId: null,
  projects: [],
  row: { 'data-tree-row': 's1', 'aria-level': 1, 'aria-posinset': 1, 'aria-setsize': 1, tabIndex: 0 },
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

/** 選單關掉、焦點要回來的那一刻。 */
function closeMenu(): Event {
  const event = new Event('focusScope.autoFocusOnUnmount', { cancelable: true });
  captured.onCloseAutoFocus?.(event);
  return event;
}

describe('SessionItem：選單關掉後的焦點', () => {
  beforeEach(() => {
    captured.onCloseAutoFocus = undefined;
    (document.activeElement as HTMLElement | null)?.blur();
  });

  it('焦點沒有被移走：回到這一列', () => {
    render(<SessionItem {...props} />);
    const event = closeMenu();
    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(screen.getByRole('treeitem'));
  });

  it('淡出期間焦點已經移到輸入框：留在輸入框，不拉回這一列', () => {
    render(<><SessionItem {...props} /><textarea aria-label="輸入框" /></>);
    const composer = screen.getByLabelText('輸入框');
    composer.focus();
    const event = closeMenu();
    // Radix 自己的預設仍要擋下：不然它會把焦點給列裡的「⋯」，一樣搶走
    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(composer);
  });

  it('焦點還在這一列裡面（例如「⋯」）：回到這一列', () => {
    render(<SessionItem {...props} />);
    const row = screen.getByRole('treeitem');
    const trigger = row.querySelector<HTMLButtonElement>('button[tabindex="-1"]');
    expect(trigger).not.toBeNull();
    trigger!.focus();
    expect(document.activeElement).toBe(trigger);
    const event = closeMenu();
    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(row);
  });

  it('選了重命名：焦點給輸入框', () => {
    render(<SessionItem {...props} editingSessionId="s1" editTitle="這一段" />);
    closeMenu();
    expect(document.activeElement).toBe(screen.getByRole('textbox'));
  });
});
