import { describe, expect, it } from 'vitest';
import { applySessionChange, mergeSessionRefresh, parseSaveResult, isSaved, parseSessionSnapshot } from '../session_persistence';
import type { SaveResult, Session } from '../types';
import { ERRORS } from '../error_codes';

const session = (id = 'a'): Session => ({ id, title: id, messages: [], updatedAt: 1 });
describe('pure persistence transition and generated result boundary', () => {
  it.each([
    { status: 'saved', representation: 'full', revision: 'r1' },
    { status: 'saved', representation: 'text', revision: 'r1' },
    { status: 'skipped', reason: 'no_text' },
    { status: 'skipped', reason: 'incognito' },
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
  it('text/no_text are not full-tree success', () => {
    expect(isSaved({ status: 'saved', representation: 'text', revision: 'r' })).toBe(false);
    expect(isSaved({ status: 'skipped', reason: 'no_text' })).toBe(false);
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
