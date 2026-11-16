/**
 * ADR-0292：串流以錯誤結束時，那則回覆記下回覆失敗，畫面才看得出是模型沒有回成功。
 */
import { describe, expect, it } from 'vitest';
import { markGenerationFailed, markGenerationStopped } from '../stream_tree';
import type { TreeHistory } from '../tree_history';

const tree: TreeHistory = {
  messages: {
    user: { id: 'user', role: 'user', content: 'hi', parentId: null, childrenIds: ['reply'] },
    reply: { id: 'reply', role: 'assistant', content: '', parentId: 'user', childrenIds: [] },
  },
  currentId: 'reply',
};

describe('markGenerationFailed', () => {
  it('只標那則回覆，內容和其他節點不動', () => {
    const marked = markGenerationFailed(tree, 'reply');
    expect(marked.messages.reply).toEqual({ ...tree.messages.reply, generationFailed: true });
    expect(marked.messages.user).toBe(tree.messages.user);
    expect(marked.currentId).toBe('reply');
  });

  it('不改傳進來的樹', () => {
    markGenerationFailed(tree, 'reply');
    expect(tree.messages.reply.generationFailed).toBeUndefined();
  });

  it('找不到節點時原樣回傳', () => {
    expect(markGenerationFailed(tree, 'missing')).toBe(tree);
  });
});

// ADR-0318：使用者停止了這一輪，那則回覆記下；下一輪送給模型時帶記號
describe('markGenerationStopped', () => {
  it('只標那則回覆，不改傳進來的樹；找不到節點時原樣回傳', () => {
    const marked = markGenerationStopped(tree, 'reply');
    expect(marked.messages.reply).toEqual({ ...tree.messages.reply, generationStopped: true });
    expect(marked.messages.user).toBe(tree.messages.user);
    expect(tree.messages.reply.generationStopped).toBeUndefined();
    expect(markGenerationStopped(tree, 'missing')).toBe(tree);
  });
});
