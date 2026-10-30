import {
    mkdirSync,
    mkdtempSync,
    realpathSync,
    rmSync,
    symlinkSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
    afterAll,
    afterEach,
    beforeAll,
    describe,
    expect,
    it,
    vi,
} from 'vitest';

const { proposeChanges, readFileNoFollow, workspaceFolders, writeFileNoFollow } = vi.hoisted(() => ({
    proposeChanges: vi.fn(async (..._args: unknown[]) => 'proposed'),
    readFileNoFollow: vi.fn(async (..._args: unknown[]) => 'content'),
    workspaceFolders: [] as Array<{ uri: { fsPath: string } }>,
    writeFileNoFollow: vi.fn(async (..._args: unknown[]) => undefined),
}));

vi.mock('vscode', () => ({ workspace: { workspaceFolders } }));
vi.mock('../mcp/diffManager', () => ({ diffManager: { proposeChanges } }));
vi.mock('../mcp/noFollowFile', () => ({ readFileNoFollow, writeFileNoFollow }));

import { AuthLevel } from '../mcp/authPolicy';
import { handleReadFile, handleWriteFile } from '../mcp/handlers/FileHandlers';

describe('handleReadFile and handleWriteFile (ADR-0294)', () => {
    const sandbox = mkdtempSync(join(tmpdir(), 'toukaide-write-'));
    const workspace = join(sandbox, 'workspace');

    beforeAll(() => {
        mkdirSync(workspace);
        workspaceFolders.push({ uri: { fsPath: workspace } });
    });

    afterAll(() => {
        rmSync(sandbox, { recursive: true, force: true });
    });

    afterEach(() => {
        proposeChanges.mockClear();
        readFileNoFollow.mockClear();
        writeFileNoFollow.mockClear();
    });

    it('reads through the no-follow reader', async () => {
        await expect(handleReadFile('a.txt')).resolves.toBe('content');
        expect(readFileNoFollow).toHaveBeenCalledWith(join(realpathSync.native(workspace), 'a.txt'));
    });

    it('writes directly through the no-follow writer', async () => {
        await expect(handleWriteFile('a.txt', 'x', AuthLevel.YOLO, false)).resolves.toContain('Successfully');
        expect(writeFileNoFollow).toHaveBeenCalledWith(join(realpathSync.native(workspace), 'a.txt'), 'x');
    });

    it('gives the diff a path check that runs again on accept', async () => {
        await handleWriteFile('later.txt', 'x', AuthLevel.Manual, false);

        const [proposedPath, content, recheck] = proposeChanges.mock.calls[0] as [string, string, () => string];
        expect(content).toBe('x');
        expect(recheck()).toBe(proposedPath);

        symlinkSync(join(sandbox, 'elsewhere'), join(workspace, 'later.txt'), 'junction');
        expect(recheck).toThrow();
    });
});
