import { describe, expect, it } from 'vitest';
import fixture from '../../../../tests/fixtures/sliding_window_trim.json';
import type { Session } from '@/lib/types';
import type { TreeHistory } from '@/lib/tree_history';
import { graftOntoTrim } from '../trim_graft';

// 範例由後端真的剪裁函式驗過（test_sliding_window_service.py），兩邊不會各寫各的（ADR-0302）
const tree = (value: unknown): TreeHistory => structuredClone(value) as TreeHistory;
const session = (messages: TreeHistory, extra: Partial<Session> = {}): Session =>
  ({ id: 's', title: 'home', messages, updatedAt: 1, context_summary: 'old summary', ...extra });
const base = () => session(tree(fixture.before));
const trimmed = (edit: (messages: TreeHistory) => void = () => {}, extra: Partial<Session> = {}) => {
  const messages = tree(fixture.after);
  edit(messages);
  return session(messages, { context_summary: 'new summary', ...extra });
};
const withNext = (parent: string): Session => {
  const local = base();
  local.messages.messages[parent].childrenIds.push('u8');
  local.messages.messages.u8 = { id: 'u8', role: 'user', content: 'next', parentId: parent, childrenIds: [] };
  local.messages.currentId = 'u8';
  return local;
};

describe('graftOntoTrim（ADR-0302）', () => {
  it('下一則接在剪過的存檔上；被剪的節點、主幹外的分支與舊摘要都不寫回去', () => {
    const result = graftOntoTrim(trimmed(), base(), withNext('n7'))!;
    expect(Object.keys(result.messages.messages).sort()).toEqual(['n4', 'n5', 'n6', 'n7', 'u8']);
    expect(result.messages.messages.n4.parentId).toBeNull();
    expect(result.messages.messages.n7.childrenIds).toEqual(['u8']);
    expect(result.messages.currentId).toBe('u8');
    expect(result.context_summary).toBe('new summary');
  });

  it('這一頁新建的對話少了後端補上的欄位（模式、專案等）：只比這一頁有的欄位，照樣接', () => {
    const local = withNext('n7');
    const confirmed = base();
    for (const session of [local, confirmed]) delete session.context_summary;
    const stored = trimmed(() => {}, { auth_level: 'Manual', project_id: null, active_skills: [], active_mcp_servers: [] });
    expect(graftOntoTrim(stored, confirmed, local)?.messages.currentId).toBe('u8');
  });

  it('逾時那條路：存檔最後是這一輪的文字版，畫面還沒確認過它，換成本機的富版', () => {
    const confirmed = base();
    delete confirmed.messages.messages.n7;
    confirmed.messages.messages.n6.childrenIds = [];
    confirmed.messages.currentId = 'n6';
    const stored = trimmed(messages => { delete messages.messages.n7.steps; });
    const result = graftOntoTrim(stored, confirmed, base())!;
    expect(Object.keys(result.messages.messages).sort()).toEqual(['n4', 'n5', 'n6', 'n7']);
    expect(result.messages.messages.n7.steps).toEqual([{ tool: 'x' }]);
    expect(result.messages.messages.n7.parentId).toBe('n6');
  });

  it.each([
    ['心跳或別的裝置多寫了一則', trimmed(messages => {
      messages.messages.n7.childrenIds = ['h8'];
      messages.messages.h8 = { id: 'h8', role: 'user', content: 'heartbeat', parentId: 'n7', childrenIds: [] };
      messages.currentId = 'h8';
    })],
    ['留下的內容被改過', trimmed(messages => { messages.messages.n5.content = 'edited elsewhere'; })],
    ['標題被改過', trimmed(() => {}, { title: 'renamed elsewhere' })],
    ['沒有剪掉任何一則', session(tree(fixture.before), { context_summary: 'new summary' })],
  ])('不是剪裁造成的：%s → 照舊提示', (_name, stored) => {
    expect(graftOntoTrim(stored, base(), withNext('n7'))).toBeNull();
  });

  it.each([['被剪掉的範圍', 'n1'], ['主幹外被丟掉的分支', 'b5']])('本機新的一則接在%s：接不上，照舊提示', (_name, parent) => {
    expect(graftOntoTrim(trimmed(), base(), withNext(parent))).toBeNull();
  });

  it('本機另有一則接不上（不是目前這則）：不默默丟掉，照舊提示', () => {
    const local = withNext('n7');
    // 上一層沒記下它，「改過被剪掉的那則」抓不到，要靠接不上就停
    local.messages.messages.e2 = { id: 'e2', role: 'user', content: 'edited old turn', parentId: 'n1', childrenIds: [] };
    expect(graftOntoTrim(trimmed(), base(), local)).toBeNull();
  });

  it('本機改過被剪掉的一則：照舊提示', () => {
    const local = base();
    local.messages.messages.n2.content = 'edited here';
    expect(graftOntoTrim(trimmed(), base(), local)).toBeNull();
  });
});
