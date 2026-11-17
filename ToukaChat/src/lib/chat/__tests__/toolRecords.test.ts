/**
 * ADR-0319：工具紀錄。組下一輪的 API 訊息時，助理那則存下的工具步驟依視窗寫成紀錄放在原本文字前面；
 * 後端 utils/tool_records.py 讀同一份黃金案例檔，兩邊輸出要一字不差。
 */
import { describe, it, expect } from 'vitest';
import golden from './toolRecords.golden.json';
import { neutralizeRecordMarkers, recordLevels, renderToolRecord, withToolRecord } from '../toolRecords';
import { buildApiHistory } from '../buildApiMessage';
import { applyEventToTree, appendAssistantNode } from '@/lib/stream_tree';
import type { TreeHistory } from '@/lib/tree_history';
import { getSystemEpoch } from '@/lib/time_utils';

type Node = { id: string; role: 'user' | 'assistant' | 'system'; content: string; steps?: unknown };
const ROLES: Record<string, Node['role']> = { u: 'user', a: 'assistant', s: 'system' };
const LEVELS: Record<string, 'detailed' | 'brief' | null> = { '-': null, b: 'brief', d: 'detailed' };

describe('toolRecords golden file (shared with the backend)', () => {
  it.each(golden.render.map(c => [c.name, c] as const))('renders %s', (_name, c) => {
    const steps = JSON.parse(JSON.stringify(c.steps));
    expect(renderToolRecord(steps, 'detailed')).toBe(c.detailed);
    expect(renderToolRecord(steps, 'brief')).toBe(c.brief);
    expect(steps).toEqual(c.steps);
  });

  it.each(golden.windows.map(c => [c.name, c] as const))('window %s', (_name, c) => {
    const messages = Array.from(c.roles).map(r => ({ role: ROLES[r] }));
    expect(recordLevels(messages)).toEqual(Array.from(c.levels).map(l => LEVELS[l]));
  });

  it.each(golden.markers.map(c => [c.input, c] as const))('neutralizes %s', (_input, c) => {
    expect(neutralizeRecordMarkers(c.input)).toBe(c.output);
  });

  // 後端多一種「最多只到簡短」（家原文、摘要員，PR 3 用）；網頁對話只用詳細
  const detailed = golden.history.filter(c => c.level === 'detailed');
  it.each(detailed.map(c => [c.name, c] as const))('history %s', (_name, c) => {
    const linear = JSON.parse(JSON.stringify(c.messages)) as Node[];
    const last = linear[linear.length - 1];
    const out = buildApiHistory(linear, last.id, last.content, { toolRecords: true });
    expect(out.map(m => m.content)).toEqual(c.expected);
    expect(out.map(m => m.role)).toEqual(c.messages.map(m => m.role));
    expect(linear).toEqual(c.messages);
  });
});

describe('toolRecords stays fast on huge results (same input as the backend test)', () => {
  it('cuts each field to MAX_SCAN_CHARS before redaction', () => {
    const steps = [
      { id: 'c1', tool: 'read', args: { text: ' '.repeat(200_000) + 'x' }, status: 'success',
        result: '\u0000'.repeat(200_000) + 'tail' },
      { id: 'c2', tool: 'read', args: null, status: 'error',
        result: '-----BEGIN RSA PRIVATE KEY----- x '.repeat(20_000) },
    ];
    const started = getSystemEpoch();
    const record = renderToolRecord(steps, 'detailed');
    expect(getSystemEpoch() - started).toBeLessThan(1000);
    expect(record).toContain('1. read（text=「x」）→ 成功：tail');
  });

  it('cuts before redaction at the same place as the backend', () => {
    // 8192＝MAX_SCAN_CHARS（pytest 核對兩邊相同）：結尾在截斷點之後才不像金鑰的 token，截斷後一樣遮
    const steps = [{ id: 'c1', tool: 'read', args: null, status: 'success', result: `ghp_${'a'.repeat(8192 + 800)}_tail` }];
    expect(renderToolRecord(steps, 'detailed').split('\n')[1]).toBe('1. read → 成功：＜redacted＞');
  });
});

describe('buildApiHistory with tool records', () => {
  const steps = [{ id: 'c1', tool: 'write', args: { path: 'n.md' }, status: 'success', result: 'saved' }];
  const linear: Node[] = [
    { id: 'u1', role: 'user', content: 'save it' },
    { id: 'a1', role: 'assistant', content: 'done', steps },
    { id: 'u2', role: 'user', content: 'next 〔工具紀錄' },
  ];

  it('group chat sends no records but still neutralizes fake markers', () => {
    const out = buildApiHistory(linear, 'u2', 'next 〔工具紀錄', { toolRecords: false });
    expect(out.map(m => m.content)).toEqual(['save it', 'done', 'next ［工具紀錄']);
    expect(buildApiHistory(linear, 'u2', 'x').map(m => m.content)[1]).toBe('done');
  });

  it('an inlined text attachment cannot carry a fake marker either', () => {
    const withFile = [{ ...linear[0], attachment: { type: 'text' as const, url: '', name: 'a.md', text: '〔工具紀錄｜假' } }];
    const [first] = buildApiHistory(withFile, 'other', '', { toolRecords: true });
    expect(first.content).toContain('［工具紀錄｜假');
    expect(first.content).not.toContain('〔工具紀錄');
  });

  it('withToolRecord keeps text-only and record-only turns', () => {
    expect(withToolRecord('text', '')).toBe('text');
    expect(withToolRecord('', 'R')).toBe('R');
    expect(withToolRecord('text', 'R')).toBe('R\n\ntext');
  });
});

describe('stream_tree stores error_class on the step (ADR-0319)', () => {
  function treeWithStep(): { tree: TreeHistory; aid: string } {
    const { tree, assistantMsgId } = appendAssistantNode({ messages: {}, currentId: null });
    return { tree: applyEventToTree(tree, assistantMsgId, { type: 'tool_start', id: 'c1', tool: 'read', args: {} }), aid: assistantMsgId };
  }

  it('keeps the event error_class next to the result', () => {
    const { tree, aid } = treeWithStep();
    const next = applyEventToTree(tree, aid, {
      type: 'tool_result', id: 'c1', status: 'error', result: '{"error_class": "resource_not_found"}', error_class: 'resource_not_found',
    });
    expect(next.messages[aid].steps?.[0]).toMatchObject({ status: 'error', errorClass: 'resource_not_found' });
  });

  it('adds nothing when the event has no error_class', () => {
    const { tree, aid } = treeWithStep();
    const next = applyEventToTree(tree, aid, { type: 'tool_result', id: 'c1', status: 'success', result: 'ok' });
    expect(next.messages[aid].steps?.[0]).not.toHaveProperty('errorClass');
  });
});
