import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { MessageItem } from '@/components/chat/MessageItem';
import { MessageNode, TreeHistory } from '@/lib/tree_history';

// Mock connections.ts
vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://localhost:8000',
  fetchTouka: vi.fn(),
}));

describe('MessageItem ApplyButton', () => {
  const mockMsg: MessageNode = {
    id: 'msg_1',
    parentId: null,
    childrenIds: [],
    role: 'assistant',
    content: 'Here is some code:\n```js\nconsole.log("hello");\n```',
    createdAt: 1234567890
  };

  const mockHistory: TreeHistory = {
    messages: {
      'msg_1': mockMsg
    },
    currentId: 'msg_1'
  };

  const defaultProps = {
    msg: mockMsg,
    history: mockHistory,
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
    handleApplyCode: vi.fn(() => new Promise<boolean>(() => {})) // 永遠處於 pending 狀態以測試 loading
  };

  it('TDD-Red: renders APPLY button, and when clicked, entering loading state without showing ⏳ emoji', async () => {
    render(<MessageItem {...defaultProps} />);
    
    // 尋找 APPLY 按鈕
    const applyButton = screen.getByRole('button', { name: /APPLY/i });
    expect(applyButton).toBeInTheDocument();
    
    // 點擊套用
    fireEvent.click(applyButton);
    
    // 此時應該進入 loading 狀態 (因為 handleApplyCode 是一個未 resolved 的 Promise)
    // TDD 斷言：不應該含有 ⏳ 表情符號
    expect(applyButton.textContent).not.toContain('⏳');
    
    // 且按鈕此時應該 disabled
    expect(applyButton).toBeDisabled();
    
    // 且應包含一個 animate-spin 的 Lucide 圖示或圖示容器
    const svg = applyButton.querySelector('svg');
    expect(svg).toBeInTheDocument();
    expect(svg?.classList.contains('animate-spin')).toBe(true);
  });

  it('TDD-Red: renders code block inside split bubbles when msg.content contains special agent tags, and it must have COPY and APPLY buttons', () => {
    const msgWithSplitBubble: MessageNode = {
      id: 'msg_2',
      parentId: null,
      childrenIds: [],
      role: 'assistant',
      content: '**【愛麗絲】**：\nHere is code:\n```js\nconsole.log("alice");\n```',
      createdAt: 1234567890
    };

    const splitHistory: TreeHistory = {
      messages: {
        'msg_2': msgWithSplitBubble
      },
      currentId: 'msg_2'
    };

    render(<MessageItem {...defaultProps} msg={msgWithSplitBubble} history={splitHistory} />);

    // 驗證是否有渲染分割氣泡標題 "愛麗絲"
    expect(screen.getByText('愛麗絲')).toBeInTheDocument();

    // 驗證分割氣泡內的程式碼區塊也有 APPLY 按鈕
    const applyButton = screen.getByRole('button', { name: /APPLY/i });
    expect(applyButton).toBeInTheDocument();
  });

  it('TDD-Red: maps Chinese agent names to normalized lowercase IDs for CSS variables (e.g., 愛麗絲 -> alice)', () => {
    const msgWithChineseAgent: MessageNode = {
      id: 'msg_3',
      parentId: null,
      childrenIds: [],
      role: 'assistant',
      content: '**【愛麗絲】**：\nHello',
      createdAt: 1234567890
    };

    const chineseHistory: TreeHistory = {
      messages: {
        'msg_3': msgWithChineseAgent
      },
      currentId: 'msg_3'
    };

    const { container } = render(<MessageItem {...defaultProps} msg={msgWithChineseAgent} history={chineseHistory} />);

    // 獲取渲染出來的 Agent 標籤容器，並驗證其顏色指向 normalized 小寫 ID alice
    const agentTag = screen.getByText('愛麗絲');
    expect(agentTag).toBeInTheDocument();

    // 檢查它的 style 屬性是否包含 --color-agent-alice
    const styleAttr = agentTag.getAttribute('style');
    expect(styleAttr).toContain('--color-agent-alice');
  });
});
