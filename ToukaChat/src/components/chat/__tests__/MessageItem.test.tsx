import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MessageItem } from '../MessageItem';
import React from 'react';

const translation = vi.hoisted(() => ({ current: null as ((key: string) => string) | null }));
vi.mock('react-i18next', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-i18next')>();
  return {
    ...actual,
    useTranslation: () => {
      const result = actual.useTranslation();
      return { ...result, t: translation.current ?? result.t };
    },
  };
});

// Mock dependencies
vi.mock('../ToolStepTracker', () => ({
  ToolStepTracker: () => <div data-testid="tool-step-tracker" />
}));

vi.mock('next/image', () => ({
  default: (props: any) => <img {...props} />
}));

const mockHistory = {
  messages: {
    '1': { id: '1', role: 'user', content: 'hello', childrenIds: ['2'] },
    '2': { id: '2', role: 'assistant', content: 'hi', parentId: '1', childrenIds: [] }
  },
  currentId: '2'
};

const defaultProps = {
  history: mockHistory as any,
  isSending: false,
  isIDE: false,
  editingMessageId: null,
  setEditingMessageId: vi.fn(),
  editValue: '',
  setEditValue: vi.fn(),
  handleCopy: vi.fn(),
  handleEditMessage: vi.fn(),
  handleDeleteMessage: vi.fn(),
  handleRegenerate: vi.fn(),
  handleSwitchMessage: vi.fn(),
  handleApplyCode: vi.fn().mockResolvedValue(true)
};

describe('MessageItem', () => {
  beforeEach(() => { translation.current = null; });

  it('P11 切換語言後，程式碼複製按鈕更新翻譯且保留複製行為', () => {
    const msg = { ...mockHistory.messages['2'], role: 'assistant' as const, content: '```python\nprint("hi")\n```' };
    translation.current = (key: string) => `before:${key}`;
    const { rerender } = render(<MessageItem {...defaultProps} msg={msg} />);
    expect(screen.getByTitle('before:chat.message_item.copy_code')).toBeInTheDocument();

    translation.current = (key: string) => `after:${key}`;
    // 新訊息參照觸發 React.memo 重繪；兩個 markdown callback 依賴保持不變。
    rerender(<MessageItem {...defaultProps} msg={{ ...msg }} />);
    fireEvent.click(screen.getByTitle('after:chat.message_item.copy_code'));
    expect(defaultProps.handleCopy).toHaveBeenLastCalledWith('print("hi")');
  });

  it('renders user message correctly', () => {
    const msg = mockHistory.messages['1'] as any;
    render(<MessageItem {...defaultProps} msg={msg} />);
    expect(screen.getByText('hello')).toBeInTheDocument();
    expect(screen.getByText('You')).toBeInTheDocument();
  });

  it('ADR-0294 外部圖片不自動載入，改顯示佔位連結', () => {
    const msg = { ...mockHistory.messages['2'], content: '![x](https://evil.example/a.png?d=1)' } as any;
    const { container } = render(<MessageItem {...defaultProps} msg={msg} />);
    expect(container.querySelector('img[src*="evil.example"]')).toBeNull();
    expect(screen.getByRole('link', { name: /外部圖片（未載入）/ })).toHaveAttribute('target', '_blank');
  });

  it('renders assistant message with markdown', () => {
    const msg = { ...mockHistory.messages['2'], content: '# Head\n**bold**' } as any;
    render(<MessageItem {...defaultProps} msg={msg} />);
    expect(screen.getByRole('heading')).toHaveTextContent('Head');
    expect(screen.getByText('bold').tagName).toBe('STRONG');
  });

  it('renders specialized ToukaGroup multi-model content', () => {
    const msg = { 
        id: 'mock-1',
        role: 'assistant', 
        content: '**【ModelA】**：Content A\n---\n**【ModelB】**：Content B',
        childrenIds: []
    } as any;
    render(<MessageItem {...defaultProps} msg={msg} />);
    expect(screen.getByText('ModelA')).toBeInTheDocument();
    expect(screen.getByText('ModelB')).toBeInTheDocument();
    expect(screen.getByText('Content A')).toBeInTheDocument();
    expect(screen.getByText('Content B')).toBeInTheDocument();
  });

  it('triggers handleCopy when copy button is clicked', () => {
    const msg = mockHistory.messages['1'] as any;
    render(<MessageItem {...defaultProps} msg={msg} />);
    
    // In the real code, copy button is visible on hover. 
    // We just find it by title.
    const copyBtn = screen.getByTitle('複製');
    fireEvent.click(copyBtn);
    expect(defaultProps.handleCopy).toHaveBeenCalledWith('hello');
  });

  it('enters edit mode when edit button is clicked', () => {
    const msg = mockHistory.messages['1'] as any;
    render(<MessageItem {...defaultProps} msg={msg} />);
    
    const editBtn = screen.getByTitle('編輯');
    fireEvent.click(editBtn);
    expect(defaultProps.setEditingMessageId).toHaveBeenCalledWith('1');
    expect(defaultProps.setEditValue).toHaveBeenCalledWith('hello');
  });

  it('renders code block with APPLY button and handles apply', async () => {
    const msg = { 
        id: 'mock-2',
        role: 'assistant', 
        content: '```python\nprint("hi")\n```',
        childrenIds: []
    } as any;
    render(<MessageItem {...defaultProps} msg={msg} />);
    
    expect(screen.getByText('python')).toBeInTheDocument();
    const applyBtn = screen.getByText('APPLY');
    fireEvent.click(applyBtn);
    
    expect(defaultProps.handleApplyCode).toHaveBeenCalledWith('print("hi")', 'python');
    
    // Check loading state (simulated)
    expect(await screen.findByText('OK')).toBeInTheDocument();
  });
});
