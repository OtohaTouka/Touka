import {
    mkdirSync,
    mkdtempSync,
    rmSync,
    symlinkSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
    afterAll,
    beforeAll,
    describe,
    expect,
    it,
} from 'vitest';

import { resolveWorkspacePath } from '../mcp/workspacePath';

// 連結用 junction：Windows 不用系統管理員就建得了（也能指向不存在的位置），POSIX 上 Node 忽略這個型別、建一般的 symlink。
describe('resolveWorkspacePath with dangling links (ADR-0294)', () => {
    const sandbox = mkdtempSync(join(tmpdir(), 'toukaide-path-security-'));
    const workspace = join(sandbox, 'workspace');
    const outside = join(sandbox, 'outside');

    beforeAll(() => {
        mkdirSync(workspace);
        mkdirSync(outside);
        mkdirSync(join(workspace, 'inside'));
        symlinkSync(join(outside, 'not-yet'), join(workspace, 'dangling-out'), 'junction');
        symlinkSync(join(workspace, 'missing'), join(workspace, 'dangling-in'), 'junction');
        symlinkSync(join(workspace, 'inside'), join(workspace, 'inside-link'), 'junction');
    });

    afterAll(() => {
        rmSync(sandbox, { recursive: true, force: true });
    });

    it('rejects a path whose last segment is a dangling link', () => {
        expect(() => resolveWorkspacePath([workspace], 'dangling-out')).toThrow();
    });

    it('rejects a path that goes through a dangling link', () => {
        expect(() => resolveWorkspacePath([workspace], join('dangling-out', 'new.txt'))).toThrow();
    });

    it('rejects a dangling link even when it points inside the workspace', () => {
        expect(() => resolveWorkspacePath([workspace], join('dangling-in', 'new.txt'))).toThrow();
    });

    it('still resolves a link that points to an existing folder inside the workspace', () => {
        expect(resolveWorkspacePath([workspace], join('inside-link', 'new.txt')))
            .toBe(join(workspace, 'inside', 'new.txt'));
    });

    it('still accepts a new file under a missing folder inside the workspace', () => {
        expect(resolveWorkspacePath([workspace], join('a', 'b', 'new.txt')))
            .toBe(join(workspace, 'a', 'b', 'new.txt'));
    });
});
