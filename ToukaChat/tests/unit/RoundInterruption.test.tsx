/**
 * ADR-0249 第 4b 段（U4b-2）：時間軸標「這一輪被你打斷」與 Ctrl+Enter 的說明文字。
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import i18n from '@/lib/i18n';
import { InterjectionMarker } from '@/components/chat/InterjectionMarker';
import { MessageItem } from '@/components/chat/MessageItem';
import type { MessageNode } from '@/lib/tree_history';
import type { Interjection } from '@/lib/types';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://localhost:8000',
  fetchTouka: vi.fn(),
}));

const delivered = (id: string, step: number, text: string): Interjection =>
  ({ id, text, urgent: true, status: 'delivered', step, created_at: 1 });

describe('InterjectionMarker：被你打斷的那一輪', () => {
  it('標出第 N 步被打斷，與送達的補充照步數排（同一步先列打斷）', () => {
    render(<InterjectionMarker interjections={[delivered('a', 3, '改看 B'), delivered('b', 1, '先查 A')]}
      interruptions={[{ step: 3 }]} />);
    const rows = within(screen.getByRole('list', { name: '本回合中的補充' })).getAllByRole('listitem');
    expect(rows.map(r => r.textContent)).toEqual([
      '你的補充在第 1 步送達先查 A',
      '第 3 步：這一輪被你打斷',
      '你的補充在第 3 步送達改看 B',
    ]);
  });

  it('只有打斷、沒有送達的補充也會顯示', () => {
    render(<InterjectionMarker interjections={[]} interruptions={[{ step: 2 }]} />);
    expect(screen.getByText('第 2 步：這一輪被你打斷')).toBeInTheDocument();
  });
});

describe('MessageItem：從存檔的節點讀回仍看得到', () => {
  const node: MessageNode = {
    id: 'a1', parentId: null, childrenIds: [], role: 'assistant', content: '最後的回答',
    interruptions: [{ step: 4 }],
  };

  it('節點帶 interruptions 時在時間軸顯示', () => {
    render(<MessageItem msg={node} history={{ messages: { a1: node }, currentId: 'a1' }} isSending={false}
      isIDE={false} editingMessageId={null} setEditingMessageId={vi.fn()} editValue="" setEditValue={vi.fn()}
      handleCopy={vi.fn()} handleEditMessage={vi.fn()} handleDeleteMessage={vi.fn()} handleRegenerate={vi.fn()}
      handleSwitchMessage={vi.fn()} handleApplyCode={vi.fn(async () => true)} />);
    expect(screen.getByText('第 4 步：這一輪被你打斷')).toBeInTheDocument();
  });
});

describe('Ctrl+Enter 的說明文字', () => {
  it('兩語都改成「立即中斷正在寫的回答」，Enter、Esc 說法不變', () => {
    expect(i18n.getFixedT('zh-TW')('chat.interjection.generating_hint'))
      .toBe('AI 工作中：Enter 排隊補充，Ctrl+Enter 立即中斷正在寫的回答，Esc 停止');
    expect(i18n.getFixedT('en')('chat.interjection.generating_hint'))
      .toBe('AI is working: Enter queues a message, Ctrl+Enter immediately interrupts the reply being written, Esc stops');
  });
});
