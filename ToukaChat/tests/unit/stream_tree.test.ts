/**
 * ADR-0080 背景續跑：串流樹純函式單元測試（先 Red 後 Green）。
 *
 * stream_tree.ts 把「串流回調如何更新對話樹」抽為不依賴 React 的純函式，
 * 供 StreamRegistryProvider 使用、可獨立單測（SRP / SSOT：樹變更邏輯單一來源）。
 */
import { describe, it, expect } from 'vitest';
import {
  buildStreamNodes,
  buildUserTurnTree,
  appendAssistantNode,
  applyChunkToTree,
  applyEventToTree,
  restoreAssistantNode,
} from '@/lib/stream_tree';
import { TreeHistory } from '@/lib/tree_history';

const emptyTree: TreeHistory = { messages: {}, currentId: null };

describe('fixed generation replay tree', () => {
  it('keeps one canonical assistant id, parent children and rich extensions', () => {
    const tree = { extension: { keep: true }, messages: {
      u: { id: 'u', parentId: null, childrenIds: ['a'], role: 'user' as const, content: 'q' },
      a: { id: 'a', parentId: 'u', childrenIds: [], role: 'assistant' as const, content: 'text', extension: 'kept' },
    }, currentId: 'a' };
    const restored = restoreAssistantNode(tree, 'u', 'a');
    expect(restored).toEqual(tree);
    expect(restored.messages.u.childrenIds).toEqual(['a']);
    expect(Object.keys(restored.messages)).toEqual(['u', 'a']);
  });
  it('does not lose tree extensions during the initial user and assistant append', () => {
    const tree = { ...emptyTree, extension: 'keep' };
    expect(buildUserTurnTree(tree, { userText: 'q', attachment: null, parentId: null }).tree).toHaveProperty('extension', 'keep');
    expect(appendAssistantNode(tree).tree).toHaveProperty('extension', 'keep');
  });
  it('refuses a different current branch rather than applying an old producer to it', () => {
    const tree: TreeHistory = { messages: {
      u: { id: 'u', parentId: null, childrenIds: [], role: 'user', content: 'q' },
      other: { id: 'other', parentId: null, childrenIds: [], role: 'user', content: 'new' },
    }, currentId: 'other' };
    expect(() => restoreAssistantNode(tree, 'u', 'a')).toThrow();
  });
});

describe('buildStreamNodes', () => {
  it('在空樹上建立 user + assistant 兩節點，currentId 指向 assistant', () => {
    const { tree, userMsgId, assistantMsgId } = buildStreamNodes(emptyTree, {
      userText: '你好',
      attachment: null,
      parentId: null,
    });
    expect(tree.messages[userMsgId].role).toBe('user');
    expect(tree.messages[userMsgId].content).toBe('你好');
    expect(tree.messages[assistantMsgId].role).toBe('assistant');
    expect(tree.messages[assistantMsgId].content).toBe('');
    expect(tree.messages[assistantMsgId].parentId).toBe(userMsgId);
    expect(tree.currentId).toBe(assistantMsgId);
  });

  it('掛在既有父節點下並維護 childrenIds 雙向連結（分支）', () => {
    const base: TreeHistory = {
      messages: {
        p: { id: 'p', parentId: null, childrenIds: [], role: 'user', content: '舊' },
      },
      currentId: 'p',
    };
    const { tree, userMsgId } = buildStreamNodes(base, {
      userText: '新分支',
      attachment: null,
      parentId: 'p',
    });
    expect(tree.messages.p.childrenIds).toContain(userMsgId);
    expect(tree.messages[userMsgId].parentId).toBe('p');
  });

  it('附件存入 user 節點', () => {
    const { tree, userMsgId } = buildStreamNodes(emptyTree, {
      userText: '看圖',
      attachment: { type: 'image', url: 'http://x/y.png' },
      parentId: null,
    });
    expect(tree.messages[userMsgId].attachment).toEqual({ type: 'image', url: 'http://x/y.png' });
  });
});

describe('buildUserTurnTree（ADR-0081：user 回合先行持久化的基底）', () => {
  it('只建 user 節點、currentId 指向 user、不含 assistant', () => {
    const { tree, userMsgId } = buildUserTurnTree(emptyTree, {
      userText: '你好', attachment: null, parentId: null,
    });
    expect(tree.messages[userMsgId].role).toBe('user');
    expect(tree.messages[userMsgId].childrenIds).toEqual([]);
    expect(tree.currentId).toBe(userMsgId);
    expect(Object.values(tree.messages).some(m => m.role === 'assistant')).toBe(false);
  });

  it('掛在既有父節點下維護雙向連結', () => {
    const base: TreeHistory = {
      messages: { p: { id: 'p', parentId: null, childrenIds: [], role: 'user', content: '舊' } },
      currentId: 'p',
    };
    const { tree, userMsgId } = buildUserTurnTree(base, {
      userText: '分支', attachment: null, parentId: 'p',
    });
    expect(tree.messages.p.childrenIds).toContain(userMsgId);
  });
});

describe('appendAssistantNode（ADR-0081：resume 重建用）', () => {
  it('在 currentId 下接出空 assistant 節點並更新 currentId', () => {
    const { tree: userTree, userMsgId } = buildUserTurnTree(emptyTree, {
      userText: 'q', attachment: null, parentId: null,
    });
    const { tree, assistantMsgId } = appendAssistantNode(userTree);
    expect(tree.messages[assistantMsgId].role).toBe('assistant');
    expect(tree.messages[assistantMsgId].content).toBe('');
    expect(tree.messages[assistantMsgId].parentId).toBe(userMsgId);
    expect(tree.messages[userMsgId].childrenIds).toContain(assistantMsgId);
    expect(tree.currentId).toBe(assistantMsgId);
  });

  it('buildStreamNodes 與「buildUserTurnTree + appendAssistantNode」結果結構一致', () => {
    const composed = (() => {
      const u = buildUserTurnTree(emptyTree, { userText: 'x', attachment: null, parentId: null });
      const a = appendAssistantNode(u.tree);
      return { roles: Object.values(a.tree.messages).map(m => m.role).sort(), currentRole: a.tree.messages[a.tree.currentId!].role };
    })();
    const direct = (() => {
      const r = buildStreamNodes(emptyTree, { userText: 'x', attachment: null, parentId: null });
      return { roles: Object.values(r.tree.messages).map(m => m.role).sort(), currentRole: r.tree.messages[r.tree.currentId!].role };
    })();
    expect(composed).toEqual(direct);
  });
});

describe('applyChunkToTree', () => {
  it('把累積文字寫入指定 assistant 節點 content', () => {
    const { tree, assistantMsgId } = buildStreamNodes(emptyTree, {
      userText: 'q', attachment: null, parentId: null,
    });
    const next = applyChunkToTree(tree, assistantMsgId, '部分回應');
    expect(next.messages[assistantMsgId].content).toBe('部分回應');
  });

  it('目標節點不存在 → 原樹返回（不塞殘缺節點）', () => {
    const next = applyChunkToTree(emptyTree, 'ghost', 'x');
    expect(next).toBe(emptyTree);
    expect(next.messages.ghost).toBeUndefined();
  });
});

describe('applyEventToTree', () => {
  const setup = () => buildStreamNodes(emptyTree, { userText: 'q', attachment: null, parentId: null });

  it('tool_start 追加 running 步驟', () => {
    const { tree, assistantMsgId } = setup();
    const next = applyEventToTree(tree, assistantMsgId, { type: 'tool_start', id: 't1', tool: 'bash', args: {} });
    expect(next.messages[assistantMsgId].steps).toHaveLength(1);
    expect(next.messages[assistantMsgId].steps![0]).toMatchObject({ id: 't1', tool: 'bash', status: 'running' });
  });

  it('tool_result 將對應步驟標記 success 並帶 result', () => {
    const { tree, assistantMsgId } = setup();
    const t1 = applyEventToTree(tree, assistantMsgId, { type: 'tool_start', id: 't1', tool: 'bash', args: {} });
    const t2 = applyEventToTree(t1, assistantMsgId, { type: 'tool_result', id: 't1', status: 'success', result: 'ok' });
    expect(t2.messages[assistantMsgId].steps![0]).toMatchObject({ id: 't1', status: 'success', result: 'ok' });
  });

  it('tool_stream append 到對應步驟 result', () => {
    const { tree, assistantMsgId } = setup();
    const t1 = applyEventToTree(tree, assistantMsgId, { type: 'tool_start', id: 't1', tool: 'bash', args: {} });
    const t2 = applyEventToTree(t1, assistantMsgId, { type: 'tool_stream', id: 't1', chunk: 'line1\n' });
    const t3 = applyEventToTree(t2, assistantMsgId, { type: 'tool_stream', id: 't1', chunk: 'line2\n' });
    expect(t3.messages[assistantMsgId].steps![0].result).toBe('line1\nline2\n');
  });

  it('touka_progress 收斂進 progress map（依 kind）', () => {
    const { tree, assistantMsgId } = setup();
    const next = applyEventToTree(tree, assistantMsgId, { type: 'touka_progress', kind: 'react_slots', current: 1, total: 5 });
    expect(next.messages[assistantMsgId].progress?.react_slots).toMatchObject({ kind: 'react_slots', current: 1, total: 5 });
  });

  it('目標節點不存在 → 原樹返回', () => {
    const next = applyEventToTree(emptyTree, 'ghost', { type: 'tool_start', id: 't', tool: 'x', args: {} });
    expect(next).toBe(emptyTree);
  });
});
