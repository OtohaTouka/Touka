import { describe, expect, it } from 'vitest';
import { applySessionChange, applySetHome, matchesSessionContent, mergeSessionRefresh, parseSaveResult, isSaved, parseSessionSnapshot,
  savedPastTerminal, sessionWritePayload } from '../session_persistence';
import type { SaveResult, Session, StreamTerminalEvent } from '../types';
import { ERRORS } from '../error_codes';

const session = (id = 'a'): Session => ({ id, title: id, messages: [], updatedAt: 1 });
describe('pure persistence transition and generated result boundary', () => {
  it.each([
    { status: 'saved', representation: 'full', revision: 'r1' },
    { status: 'saved', representation: 'text', revision: 'r1' },
    { status: 'skipped', reason: 'no_text' },
    { status: 'skipped', reason: 'incognito' },
    { status: 'skipped', reason: 'generation_failed' },
    { status: 'failed', error_code: ERRORS.SESSION_SAVE_FAILED.id },
    { status: 'unknown', error_code: ERRORS.SESSION_SAVE_FAILED.id },
  ] as SaveResult[])('valid generated union round trips $status/$representation/$reason', result => {
    expect(parseSaveResult(result)).toEqual(result);
  });
  it.each([
    null, [], {}, { status: 'saved', representation: 'full' },
    { status: 'saved', representation: 'full', revision: '' },
    { status: 'saved', representation: 'full', revision: 'r1', detail: 'secret' },
    { status: 'unknown', error_code: '1302' }, { status: 'unknown', error_code: -1 },
    { status: 'unknown', error_code: ERRORS.SESSION_SAVE_FAILED.id, revision: 'stale' },
    { status: 'skipped', reason: 'arbitrary' },
  ])('rejects malformed or extra result fields %j', result => expect(parseSaveResult(result)).toBeNull());
  it('a replay preserves input, id and incognito sovereignty', () => {
    const original = { ...session(), incognito: true };
    const before = structuredClone(original);
    const change = (current: Readonly<Session>) => ({ ...current, id: 'forged', incognito: false, title: 'changed' });
    expect(applySessionChange(original, change)).toEqual(applySessionChange(original, change));
    expect(original).toEqual(before);
    expect(applySessionChange(original, change)).toMatchObject({ id: 'a', incognito: true, title: 'changed' });
  });
  it('merge protects each unsaved local draft without mutating either input', () => {
    const local = [{ ...session(), title: 'draft' }, { ...session('i'), incognito: true }];
    const remote = [{ ...session(), title: 'old' }, session('b')];
    const before = structuredClone({ local, remote });
    const merged = mergeSessionRefresh(local, remote, new Set(['a']));
    expect(merged.find(item => item.id === 'a')?.title).toBe('draft');
    expect(merged.map(item => item.id).sort()).toEqual(['a', 'b', 'i']);
    expect({ local, remote }).toEqual(before);
  });
  it('text/no_text/generation_failed are not full-tree success', () => {
    expect(isSaved({ status: 'saved', representation: 'text', revision: 'r' })).toBe(false);
    expect(isSaved({ status: 'skipped', reason: 'no_text' })).toBe(false);
    expect(isSaved({ status: 'skipped', reason: 'generation_failed' })).toBe(false);
  });
});

describe('applySetHome follows the backend set_home rule', () => {
  const home = (id: string, homeFor: string | null, over: Partial<Session> = {}): Session =>
    ({ ...session(id), is_home: homeFor !== null, home_for_agent: homeFor, ...over });
  const after = (list: Session[], id: string) => applySetHome(list, 'new', 'Alice').find(item => item.id === id);
  it('the chosen session becomes the home of that agent', () => {
    expect(after([home('new', null, { agent_id: 'Alice' })], 'new')).toMatchObject({ is_home: true, home_for_agent: 'Alice' });
  });
  it.each([
    ['same agent', 'Alice', { agent_id: 'Alice' }],
    ['legacy prefix', 'ToukaSoulAlice', { agent_id: 'Alice' }],
    ['no agent on the session', 'Alice', { agent_id: null }],
  ])('clears both home fields of the old home: %s', (_case, homeFor, over) => {
    expect(after([home('new', null), home('old', homeFor, over)], 'old')).toMatchObject({ is_home: false, home_for_agent: null });
  });
  it.each([
    ['another agent', home('other', 'Bob', { agent_id: 'Bob' })],
    // 後端看 home_for_agent：只有 is_home 沒有歸屬的列不會被清，本機也不能改，否則和存檔對不上
    ['home flag without an owner', home('other', null, { is_home: true, agent_id: 'Alice' })],
    ['a plain session of the same agent', home('other', null, { agent_id: 'Alice' })],
  ])('leaves other sessions untouched: %s', (_case, other) => {
    expect(after([home('new', null), other], 'other')).toEqual(other);
  });
  it('does not mutate its input', () => {
    const list = [home('new', null), home('old', 'Alice')];
    const before = structuredClone(list);
    applySetHome(list, 'new', 'Alice');
    expect(list).toEqual(before);
  });
  it('stores the agent without the legacy prefix, like the backend', () => {
    expect(applySetHome([home('new', null), home('old', 'Alice')], 'new', 'ToukaSoulAlice')).toEqual([
      expect.objectContaining({ id: 'new', is_home: true, home_for_agent: 'Alice' }),
      expect.objectContaining({ id: 'old', is_home: false, home_for_agent: null }),
    ]);
  });
  it('a prefix in another case is a different agent, like the backend', () => {
    const other = home('other', 'toukasoulAlice');
    expect(applySetHome([other, home('new', null)], 'new', 'Alice')[0]).toBe(other);
  });
  it('cancelling clears every home of that agent and sets nothing', () => {
    const plain = home('plain', null);
    const list = [home('current', 'Alice'), home('legacy', 'ToukaSoulAlice'), home('other', 'Bob'), plain];
    const result = applySetHome(list, 'current', 'Alice', false);
    expect(result.slice(0, 2)).toEqual([
      expect.objectContaining({ is_home: false, home_for_agent: null }),
      expect.objectContaining({ is_home: false, home_for_agent: null }),
    ]);
    expect(result[2]).toBe(list[2]);
    expect(result[3]).toBe(plain);
  });
  it('cancelling on a session the agent does not own leaves it alone, as the backend does', () => {
    const other = home('other', 'Bob');
    expect(applySetHome([other], 'other', 'Alice', false)[0]).toBe(other);
  });
  // 家欄位只由設家改，存檔不寫它（ADR-0301）：不送出，比對內容時也不算
  it('a save never sends the home fields', () => {
    const payload = sessionWritePayload(home('s', 'Alice', { title: 't' }));
    expect(payload).toMatchObject({ id: 's', title: 't' });
    expect(payload).not.toHaveProperty('is_home');
    expect(payload).not.toHaveProperty('home_for_agent');
  });
  it('different home fields alone are the same content', () => {
    expect(matchesSessionContent(home('s', null), home('s', 'Alice'))).toBe(true);
    expect(matchesSessionContent(home('s', null), home('s', 'Alice', { title: 'other' }))).toBe(false);
  });
});

describe('savedPastTerminal decides whether a finished turn may be replayed', () => {
  const R1 = 'r1';
  const NODE = 'assistant-node';
  const terminal = (generation: StreamTerminalEvent['generation'], persistence: SaveResult): StreamTerminalEvent => ({
    type: 'touka_stream_terminal', session_id: 's', stream_id: 'stream', user_parent_id: 'user',
    assistant_node_id: NODE, generation, persistence,
  });
  const saved: SaveResult = { status: 'saved', representation: 'text', revision: R1 };
  const noText: SaveResult = { status: 'skipped', reason: 'no_text' };
  const generationFailed: SaveResult = { status: 'skipped', reason: 'generation_failed' };
  const unknown: SaveResult = { status: 'unknown', error_code: ERRORS.SESSION_SAVE_FAILED.id };
  const failed: SaveResult = { status: 'failed', error_code: ERRORS.SESSION_SAVE_FAILED.id };
  const stored = (revision: string | undefined, hasNode: boolean): Session => ({ ...session('s'), revision, messages: {
    currentId: 'user', messages: { user: { id: 'user', role: 'user', content: 'u', parentId: null, childrenIds: [] },
      ...(hasNode ? { [NODE]: { id: NODE, role: 'assistant', content: 'a', parentId: 'user', childrenIds: [] } } : {}) },
  } });
  it.each([
    // 已存文字：看存檔版本有沒有前進，和節點無關
    ['completed', 'saved', R1, false, false, saved],
    ['completed', 'saved', R1, true, false, saved],
    ['completed', 'saved', 'r2', false, true, saved],
    ['completed', 'saved', 'r2', true, true, saved],
    ['cancelled', 'saved', 'r2', true, true, saved],
    ['cancelled', 'saved', R1, true, false, saved],
    ['completed', 'saved', undefined, true, false, saved],
    // 沒有版本可比：看存檔裡有沒有這一輪的回覆
    ['completed', 'no text', R1, false, false, noText],
    ['completed', 'no text', 'r2', false, false, noText],
    ['completed', 'no text', 'r2', true, true, noText],
    // 模型失敗、後端沒寫：存檔裡還沒有這則才重播（由畫面存一次）
    ['error', 'generation failed', R1, false, false, generationFailed],
    ['error', 'generation failed', 'r2', true, true, generationFailed],
    ['error', 'unknown', R1, false, false, unknown],
    ['error', 'unknown', 'r2', false, false, unknown],
    ['error', 'unknown', 'r2', true, true, unknown],
    ['error', 'failed', 'r2', false, false, failed],
    ['error', 'failed', 'r2', true, true, failed],
    ['cancelled', 'unknown', 'r2', false, false, unknown],
    ['cancelled', 'unknown', 'r2', true, true, unknown],
    ['cancelled', 'no text', 'r2', false, false, noText],
    ['cancelled', 'no text', 'r2', true, true, noText],
  ] as const)('%s / %s, stored %s, node %s -> %s', (generation, _name, revision, hasNode, expected, persistence) => {
    expect(savedPastTerminal(terminal(generation, persistence), stored(revision, hasNode))).toBe(expected);
  });
  it('a flat message list has no such node', () => {
    expect(savedPastTerminal(terminal('error', unknown), { ...session('s'), revision: 'r2', messages: [] })).toBe(false);
  });
});

describe('strict generated session snapshot boundary', () => {
  const document = { id: 's', title: 'title', messages: { messages: {}, currentId: null, extension: { rich: true } },
    updatedAt: 1, is_home: false, home_for_agent: null, agent_id: null, project_id: null,
    auth_level: 'Manual', active_mcp_servers: [], active_skills: [], last_activity_at: 1, context_summary: null };
  it('retains legitimate rich tree extensions', () => {
    expect(parseSessionSnapshot({ exists: true, revision: 'r', session: document }, 's')?.session?.messages).toEqual(document.messages);
  });
  it.each([
    ['is_home', undefined], ['is_home', 1], ['home_for_agent', {}], ['agent_id', 4], ['project_id', []],
    ['context_summary', false], ['auth_level', 'Unrestricted'], ['active_skills', 'a'], ['active_mcp_servers', [1]],
    ['last_activity_at', 1.5], ['updatedAt', 1.5], ['last_activity_at', undefined],
  ])('rejects malformed required field %s=%j', (key, value) => {
    expect(parseSessionSnapshot({ exists: true, revision: 'r', session: { ...document, [key as string]: value } }, 's')).toBeNull();
  });
});
