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

describe('resolveWorkspacePath', () => {
    const sandbox = mkdtempSync(join(tmpdir(), 'toukaide-path-test-'));
    const workspace = join(sandbox, 'workspace');
    const outside = join(sandbox, 'outside');

    beforeAll(() => {
        mkdirSync(workspace);
        mkdirSync(outside);
        symlinkSync(outside, join(workspace, 'outside-link'), 'junction');
    });

    afterAll(() => {
        rmSync(sandbox, { recursive: true, force: true });
    });

    it('resolves a relative path inside the first workspace', () => {
        expect(resolveWorkspacePath([workspace], join('src', 'index.ts')))
            .toBe(join(workspace, 'src', 'index.ts'));
    });

    it('accepts an absolute path only when it belongs to a workspace', () => {
        const inside = join(workspace, 'src', 'index.ts');
        expect(resolveWorkspacePath([workspace], inside)).toBe(inside);
    });

    it('rejects traversal and absolute paths outside every workspace', () => {
        expect(() => resolveWorkspacePath([workspace], join('..', 'secret.txt')))
            .toThrow('Path is outside the active workspace');
        expect(() => resolveWorkspacePath([workspace], join(outside, 'secret.txt')))
            .toThrow('Path is outside the active workspace');
    });

    it('rejects a workspace symlink or junction that resolves outside', () => {
        expect(() => resolveWorkspacePath(
            [workspace],
            join('outside-link', 'secret.txt'),
        )).toThrow('Path resolves outside the active workspace');
    });

    it('fails closed when no workspace is open', () => {
        expect(() => resolveWorkspacePath([], 'src/index.ts'))
            .toThrow('No workspace folder is open');
    });
});
