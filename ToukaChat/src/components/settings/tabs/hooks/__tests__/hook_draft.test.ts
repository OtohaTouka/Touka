/**
 * ADR-0292：設定頁草稿的純函式——參數與標頭的文字格式、事件設定的增刪。
 */
import { describe, it, expect } from 'vitest';
import type { HooksConfig } from '@/lib/types';
import {
    argsToText, headersToText, newGroup, newHook, removeAt, replaceAt, textToArgs, textToHeaders, withGroups,
} from '../hook_draft';

describe('hook_draft（ADR-0292）', () => {
    it('參數一行一個、原樣保留空白，只略過空行與 CRLF 的 \\r', () => {
        expect(textToArgs('--check\r\n\n  spaced  \n')).toEqual(['--check', '  spaced  ']);
        expect(argsToText(['a', 'b c'])).toBe('a\nb c');
        expect(argsToText(undefined)).toBe('');
    });

    it('標頭一行一個「名稱: 值」；值裡的冒號保留、沒有冒號的行值是空字串', () => {
        expect(textToHeaders('X-Token: abc:def\n\nX-Empty\r\n')).toEqual({ 'X-Token': 'abc:def', 'X-Empty': '' });
        expect(headersToText({ A: '1', B: '2' })).toBe('A: 1\nB: 2');
        expect(textToHeaders(headersToText({ 'X-A': 'v' }))).toEqual({ 'X-A': 'v' });
    });

    it('新的 hook 預設啟用、逾時 30 秒；command 沒有 shell 欄位', () => {
        expect(newHook('command')).toEqual({ type: 'command', command: '', args: [], enabled: true, timeout: 30 });
        expect(newHook('http')).toEqual({ type: 'http', url: '', headers: {}, enabled: true, timeout: 30 });
        expect(newGroup('http')).toEqual({ matcher: '', hooks: [newHook('http')] });
    });

    it('withGroups 換掉一個事件；清空時把事件拿掉，不動其他事件與欄位', () => {
        const base: HooksConfig = { enabled: true, version: '1.0.0', hooks: { Stop: [newGroup('http')] } };
        const added = withGroups(base, 'PreToolUse', [newGroup('command')]);
        expect(Object.keys(added.hooks ?? {})).toEqual(['Stop', 'PreToolUse']);
        expect(added.version).toBe('1.0.0');
        expect(base.hooks).toEqual({ Stop: [newGroup('http')] });

        const removed = withGroups(added, 'Stop', []);
        expect(Object.keys(removed.hooks ?? {})).toEqual(['PreToolUse']);
    });

    it('replaceAt／removeAt 不改原陣列', () => {
        const items = ['a', 'b', 'c'];
        expect(replaceAt(items, 1, 'x')).toEqual(['a', 'x', 'c']);
        expect(removeAt(items, 0)).toEqual(['b', 'c']);
        expect(items).toEqual(['a', 'b', 'c']);
    });
});
