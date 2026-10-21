/**
 * ADR-0152 T2 — applyThinkingToTree（思考分頻的樹寫入，純函式）。
 *
 * 與 applyChunkToTree 同構：收「全量累積字串」寫入節點 thinking；
 * 目標節點不存在（已停/已清）→ 原樹返回不污染。
 */
import { describe, it, expect } from 'vitest';
import { applyThinkingToTree, buildStreamNodes } from '../stream_tree';
import { TreeHistory } from '../tree_history';

const emptyTree: TreeHistory = { messages: {}, currentId: null };

function makeTree() {
  return buildStreamNodes(emptyTree, { userText: 'hi', attachment: null, parentId: null });
}

describe('applyThinkingToTree (ADR-0152)', () => {
  it('把累積思考寫入 assistant 節點 thinking，content 不受影響', () => {
    const { tree, assistantMsgId } = makeTree();
    const next = applyThinkingToTree(tree, assistantMsgId, '正在推理…');
    expect(next.messages[assistantMsgId].thinking).toBe('正在推理…');
    expect(next.messages[assistantMsgId].content).toBe('');
  });

  it('全量覆寫語意：後一次呼叫以累積字串覆蓋前值', () => {
    const { tree, assistantMsgId } = makeTree();
    const t1 = applyThinkingToTree(tree, assistantMsgId, 'step 1');
    const t2 = applyThinkingToTree(t1, assistantMsgId, 'step 1\nstep 2');
    expect(t2.messages[assistantMsgId].thinking).toBe('step 1\nstep 2');
  });

  it('目標節點不存在 → 原樹返回（不塞殘缺節點）', () => {
    const { tree } = makeTree();
    const next = applyThinkingToTree(tree, 'nonexistent-id', 'x');
    expect(next).toBe(tree);
  });

  it('不可變更新：原樹節點未被修改', () => {
    const { tree, assistantMsgId } = makeTree();
    applyThinkingToTree(tree, assistantMsgId, 'mutating?');
    expect(tree.messages[assistantMsgId].thinking).toBeUndefined();
  });
});
