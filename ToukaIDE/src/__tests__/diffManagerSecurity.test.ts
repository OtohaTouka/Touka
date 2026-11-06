import {
    existsSync,
    mkdirSync,
    mkdtempSync,
    readdirSync,
    readFileSync,
    realpathSync,
    rmSync,
    statSync,
    symlinkSync,
    writeFileSync,
} from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { basename, dirname, join } from 'node:path';

import {
    afterEach,
    beforeEach,
    describe,
    expect,
    it,
    vi,
} from 'vitest';

const { executeCommand, showInformationMessage } = vi.hoisted(() => ({
    executeCommand: vi.fn(async (..._args: unknown[]) => undefined),
    showInformationMessage: vi.fn(),
}));

vi.mock('vscode', () => ({
    Uri: { file: (fsPath: string) => ({ fsPath }) },
    commands: { executeCommand },
    window: {
        activeTextEditor: undefined,
        showInformationMessage,
        tabGroups: { all: [], close: vi.fn() },
    },
    TabInputTextDiff: class {},
}));

import { DiffManager } from '../mcp/diffManager';

const windows = process.platform === 'win32';

function proposedFile(): string {
    const diff = executeCommand.mock.calls.find((call) => call[0] === 'vscode.diff');
    return (diff?.[2] as { fsPath: string }).fsPath;
}

describe('DiffManager (ADR-0294)', () => {
    let sandbox: string;
    let workspace: string;
    let outside: string;
    let target: string;

    beforeEach(() => {
        // 和產品一樣傳解析後的真實路徑（Linux 拿它和實際開到的位置比對）
        sandbox = realpathSync.native(mkdtempSync(join(tmpdir(), 'toukaide-diff-')));
        workspace = join(sandbox, 'workspace');
        outside = join(sandbox, 'outside');
        mkdirSync(workspace);
        mkdirSync(outside);
        writeFileSync(join(outside, 'secret.txt'), 'original');
        target = join(workspace, 'a.txt');
        writeFileSync(target, 'old');
    });

    afterEach(() => {
        rmSync(sandbox, { recursive: true, force: true });
        executeCommand.mockClear();
        showInformationMessage.mockReset();
    });

    it('keeps the proposed content in a private folder and removes the folder afterwards', async () => {
        let folder = '';
        showInformationMessage.mockImplementation(async () => {
            folder = dirname(proposedFile());
            return 'Reject (Discard)';
        });

        await expect(new DiffManager().proposeChanges(target, 'new', () => target))
            .resolves.toContain('Rejected');

        expect(dirname(folder)).toBe(tmpdir());
        expect(basename(folder)).toMatch(/^touka-diff-/);
        expect(existsSync(folder)).toBe(false);
    });

    it.skipIf(windows)('creates the private folder readable only by the user', async () => {
        let mode = 0;
        showInformationMessage.mockImplementation(async () => {
            mode = statSync(dirname(proposedFile())).mode & 0o777;
            return 'Reject (Discard)';
        });

        await new DiffManager().proposeChanges(target, 'new', () => target);

        expect(mode).toBe(0o700);
    });

    it('writes the accepted content after checking the path again', async () => {
        const recheck = vi.fn(() => target);
        showInformationMessage.mockResolvedValue('Accept (Apply)');

        await expect(new DiffManager().proposeChanges(target, 'new', recheck))
            .resolves.toContain('Success');

        expect(recheck).toHaveBeenCalledTimes(1);
        expect(readFileSync(target, 'utf8')).toBe('new');
    });

    it('does not write when the path check fails on accept', async () => {
        showInformationMessage.mockResolvedValue('Accept (Apply)');

        const result = await new DiffManager().proposeChanges(target, 'new', () => {
            throw new Error('Path resolves outside the active workspace');
        });

        expect(result).toContain('Path resolves outside the active workspace');
        expect(readFileSync(target, 'utf8')).toBe('old');
    });

    it('does not write when the path resolves somewhere else on accept', async () => {
        showInformationMessage.mockResolvedValue('Accept (Apply)');

        const result = await new DiffManager().proposeChanges(target, 'new', () => join(workspace, 'b.txt'));

        expect(result).toContain('Error applying changes');
        expect(readFileSync(target, 'utf8')).toBe('old');
        expect(existsSync(join(workspace, 'b.txt'))).toBe(false);
    });

    it('does not write through a link swapped in before accept', async () => {
        showInformationMessage.mockImplementation(async () => {
            rmSync(target);
            if (windows) {
                symlinkSync(outside, target, 'junction');
            } else {
                symlinkSync(join(outside, 'secret.txt'), target);
            }
            return 'Accept (Apply)';
        });

        const result = await new DiffManager().proposeChanges(target, 'new', () => target);

        expect(result).toContain('Refusing to write through a link');
        expect(readFileSync(join(outside, 'secret.txt'), 'utf8')).toBe('original');
    });

    it('does not show the content behind a link as the original', async () => {
        rmSync(target);
        if (windows) {
            symlinkSync(outside, target, 'junction');
        } else {
            symlinkSync(join(outside, 'secret.txt'), target);
        }

        const result = await new DiffManager().proposeChanges(target, 'new', () => target);

        expect(result).toContain('Refusing to read through a link');
        expect(executeCommand).not.toHaveBeenCalledWith('vscode.diff', expect.anything(), expect.anything(), expect.anything(), expect.anything());
    });

    it.skipIf(windows)('does not hang when the original is a FIFO', async () => {
        rmSync(target);
        execFileSync('mkfifo', [target]);

        await expect(new DiffManager().proposeChanges(target, 'new', () => target))
            .resolves.toContain('not a regular file');
    }, 3000);

    it('removes the private folder when the diff view cannot open', async () => {
        const before = readdirSync(tmpdir()).filter((name) => name.startsWith('touka-diff-'));
        executeCommand.mockRejectedValueOnce(new Error('no diff view'));

        await expect(new DiffManager().proposeChanges(target, 'new', () => target))
            .resolves.toContain('no diff view');

        const after = readdirSync(tmpdir()).filter((name) => name.startsWith('touka-diff-'));
        expect(after).toEqual(before);
    });
});
