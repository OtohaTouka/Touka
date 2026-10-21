import { describe, it, expect } from 'vitest';
import { migrateFlatToTree, getLinearMessages, TreeHistory } from '@/lib/tree_history';

describe('Tree History Migration', () => {

  it('should handle flat array format correctly', () => {
    const flatData = [
      { id: '1', parentId: null, content: 'A' },
      { id: '2', parentId: '1', content: 'B' },
      { id: '3', parentId: '2', content: 'C' }
    ];

    const result = migrateFlatToTree(flatData);

    expect(result.currentId).toBe('3');
    expect(result.messages['1'].childrenIds).toContain('2');
    expect(result.messages['2'].childrenIds).toContain('3');
    expect(result.messages['3'].childrenIds).toHaveLength(0);
  });

  it('should handle Open WebUI nested dictionary format correctly', () => {
    const openWebUIData = {
      messages: {
        'msg1': { id: 'msg1', parentId: null, role: 'user', content: 'Hello' },
        'msg2': { id: 'msg2', parentId: 'msg1', role: 'assistant', content: 'Hi' },
        'msg3_branch1': { id: 'msg3_branch1', parentId: 'msg2', role: 'user', content: 'How are you' },
        'msg3_branch2': { id: 'msg3_branch2', parentId: 'msg2', role: 'user', content: 'What time is it' }
      }
    };

    const result = migrateFlatToTree(openWebUIData);

    expect(result.messages['msg1'].childrenIds).toContain('msg2');
    expect(result.messages['msg2'].childrenIds).toContain('msg3_branch1');
    expect(result.messages['msg2'].childrenIds).toContain('msg3_branch2');
    
    expect(['msg3_branch1', 'msg3_branch2']).toContain(result.currentId);
  });

  it('should safely return empty state for null or undefined', () => {
    const result1 = migrateFlatToTree(null);
    expect(result1.currentId).toBeNull();
    
    const result2 = migrateFlatToTree(undefined);
    expect(result2.currentId).toBeNull();
  });

  it('should reconstruct parentId and build full linear history for flat array without parentId/id (M6)', () => {
    const flatDataWithoutParent = [
      { role: 'user', content: 'A' },
      { role: 'assistant', content: 'B' },
      { role: 'user', content: 'C' }
    ];

    const result = migrateFlatToTree(flatDataWithoutParent);
    const linear = getLinearMessages(result);

    // 應該要重建完整的三個節點
    expect(linear).toHaveLength(3);
    expect(linear[0].content).toBe('A');
    expect(linear[1].content).toBe('B');
    expect(linear[2].content).toBe('C');
    
    // 驗證 parent-child 關係被正確建立
    const keys = Object.keys(result.messages);
    expect(result.messages[keys[1]].parentId).toBe(keys[0]);
    expect(result.messages[keys[2]].parentId).toBe(keys[1]);
  });
});

describe('getLinearMessages 環路防禦 (cycle guard)', () => {
  it('parentId 鏈成環時安全終止、不無限迴圈', () => {
    // A -> B -> C -> A（parent 鏈三節點環）
    const hist = {
      currentId: 'A',
      messages: {
        A: { id: 'A', parentId: 'C', childrenIds: [], role: 'user', content: 'A' },
        B: { id: 'B', parentId: 'A', childrenIds: [], role: 'assistant', content: 'B' },
        C: { id: 'C', parentId: 'B', childrenIds: [], role: 'user', content: 'C' },
      },
    } as unknown as TreeHistory;

    const linear = getLinearMessages(hist);
    const ids = linear.map((m) => m.id);
    expect(new Set(ids).size).toBe(ids.length); // 無重複 => 未陷入迴圈
    expect(ids.length).toBeLessThanOrEqual(3);
    expect(ids).toContain('A');
  });

  it('childrenIds 鏈指回已訪節點時安全終止', () => {
    // A 的子為 B，B 的子為 A（向下成環）
    const hist = {
      currentId: 'A',
      messages: {
        A: { id: 'A', parentId: null, childrenIds: ['B'], role: 'user', content: 'A' },
        B: { id: 'B', parentId: 'A', childrenIds: ['A'], role: 'assistant', content: 'B' },
      },
    } as unknown as TreeHistory;

    const linear = getLinearMessages(hist);
    const ids = linear.map((m) => m.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toEqual(['A', 'B']);
  });

  it('self-loop（parentId 指向自己）安全終止', () => {
    const hist = {
      currentId: 'A',
      messages: {
        A: { id: 'A', parentId: 'A', childrenIds: [], role: 'user', content: 'A' },
      },
    } as unknown as TreeHistory;
    const linear = getLinearMessages(hist);
    expect(linear.map((m) => m.id)).toEqual(['A']);
  });

  it('self-loop（childrenIds 含自己）安全終止', () => {
    const hist = {
      currentId: 'A',
      messages: {
        A: { id: 'A', parentId: null, childrenIds: ['A'], role: 'user', content: 'A' },
      },
    } as unknown as TreeHistory;
    const linear = getLinearMessages(hist);
    expect(linear.map((m) => m.id)).toEqual(['A']);
  });

  it('正常深鏈（無環）仍完整線性化、不被環路守衛誤砍', () => {
    const messages: Record<string, unknown> = {};
    const N = 50;
    for (let i = 0; i < N; i++) {
      messages[`n${i}`] = {
        id: `n${i}`,
        parentId: i === 0 ? null : `n${i - 1}`,
        childrenIds: i === N - 1 ? [] : [`n${i + 1}`],
        role: i % 2 === 0 ? 'user' : 'assistant',
        content: `m${i}`,
      };
    }
    const hist = { currentId: 'n0', messages } as unknown as TreeHistory;
    const linear = getLinearMessages(hist);
    expect(linear.length).toBe(N);
    expect(linear[0].id).toBe('n0');
    expect(linear[N - 1].id).toBe(`n${N - 1}`);
  });
});
