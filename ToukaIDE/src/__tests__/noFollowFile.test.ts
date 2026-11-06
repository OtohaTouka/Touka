import { execFileSync } from 'node:child_process';
import {
    existsSync,
    mkdirSync,
    mkdtempSync,
    promises,
    readFileSync,
    realpathSync,
    rmSync,
    symlinkSync,
    writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
    afterEach,
    beforeEach,
    describe,
    expect,
    it,
    vi,
} from 'vitest';

import { readFileNoFollow, writeFileNoFollow } from '../mcp/noFollowFile';

const windows = process.platform === 'win32';
const linux = process.platform === 'linux';

describe('readFileNoFollow and writeFileNoFollow (ADR-0294)', () => {
    let sandbox: string;
    let workspace: string;
    let outside: string;

    beforeEach(() => {
        // 和產品一樣傳解析後的真實路徑（Linux 拿它和實際開到的位置比對）
        sandbox = realpathSync.native(mkdtempSync(join(tmpdir(), 'toukaide-nofollow-')));
        workspace = join(sandbox, 'workspace');
        outside = join(sandbox, 'outside');
        mkdirSync(workspace);
        mkdirSync(outside);
        writeFileSync(join(outside, 'secret.txt'), 'original');
    });

    afterEach(() => {
        rmSync(sandbox, { recursive: true, force: true });
    });

    it('creates a new file and overwrites an existing one', async () => {
        const target = join(workspace, 'a.txt');
        await writeFileNoFollow(target, 'first');
        expect(readFileSync(target, 'utf8')).toBe('first');
        await writeFileNoFollow(target, 'second');
        expect(readFileSync(target, 'utf8')).toBe('second');
    });

    // Windows 建一般檔案的 symlink 要系統管理員，改用指向資料夾的 junction；兩邊都要看到明確的拒絕，不是其他錯誤
    it('refuses when the last segment is a link and leaves the target alone', async () => {
        const link = join(workspace, 'link');
        if (windows) {
            symlinkSync(outside, link, 'junction');
        } else {
            symlinkSync(join(outside, 'secret.txt'), link);
        }
        await expect(writeFileNoFollow(link, 'changed')).rejects.toThrow('Refusing to write through a link');
        expect(readFileSync(join(outside, 'secret.txt'), 'utf8')).toBe('original');
    });

    it('refuses a dangling link and does not create its target', async () => {
        const link = join(workspace, 'dangling');
        symlinkSync(join(outside, 'new'), link, windows ? 'junction' : undefined);
        await expect(writeFileNoFollow(link, 'changed')).rejects.toThrow('Refusing to write through a link');
        expect(existsSync(join(outside, 'new'))).toBe(false);
    });

    it.skipIf(process.platform === 'win32')('refuses a device that opens but is not a regular file', async () => {
        await expect(writeFileNoFollow('/dev/null', 'x')).rejects.toThrow('not a regular file');
    });

    it('refuses a folder', async () => {
        await expect(writeFileNoFollow(workspace, 'x')).rejects.toThrow();
    });

    it.skipIf(windows)('does not hang on a FIFO', async () => {
        const fifo = join(workspace, 'pipe');
        execFileSync('mkfifo', [fifo]);
        await expect(writeFileNoFollow(fifo, 'x')).rejects.toThrow();
    }, 3000);

    it('reads a regular file and reports a missing one as ENOENT', async () => {
        writeFileSync(join(workspace, 'a.txt'), 'content');
        await expect(readFileNoFollow(join(workspace, 'a.txt'))).resolves.toBe('content');
        await expect(readFileNoFollow(join(workspace, 'missing.txt'))).rejects.toMatchObject({ code: 'ENOENT' });
    });

    it('refuses to read through a link', async () => {
        const link = join(workspace, 'link');
        if (windows) {
            symlinkSync(outside, link, 'junction');
        } else {
            symlinkSync(join(outside, 'secret.txt'), link);
        }
        await expect(readFileNoFollow(link)).rejects.toThrow('Refusing to read through a link');
    });

    it('refuses to read a folder', async () => {
        await expect(readFileNoFollow(workspace)).rejects.toThrow('not a regular file');
    });

    it.skipIf(windows)('does not hang reading a FIFO', async () => {
        const fifo = join(workspace, 'pipe');
        execFileSync('mkfifo', [fifo]);
        await expect(readFileNoFollow(fifo)).rejects.toThrow('not a regular file');
    }, 3000);

    // 模擬檢查之後中間的資料夾被換成連結：開到的檔案實際在別處，不讀不寫，也不先清空
    it.skipIf(!linux)('refuses when a folder on the way is a link, without emptying the file it reached', async () => {
        symlinkSync(outside, join(workspace, 'via'));
        const target = join(workspace, 'via', 'secret.txt');

        await expect(writeFileNoFollow(target, 'changed')).rejects.toThrow('the path changed while opening the file');
        await expect(readFileNoFollow(target)).rejects.toThrow('the path changed while opening the file');
        expect(readFileSync(join(outside, 'secret.txt'), 'utf8')).toBe('original');
    });

    // 讀不到 /proc 時要明確拒絕，不能回 ENOENT（差異預覽會把它當成新檔案）
    it.skipIf(!linux)('refuses with its own message when /proc cannot be read', async () => {
        const target = join(workspace, 'a.txt');
        writeFileSync(target, 'content');
        const readlink = vi.spyOn(promises, 'readlink').mockRejectedValue(
            Object.assign(new Error('no /proc'), { code: 'ENOENT' }),
        );
        try {
            const error = await readFileNoFollow(target).catch((caught: NodeJS.ErrnoException) => caught);
            expect(error).toBeInstanceOf(Error);
            expect((error as NodeJS.ErrnoException).message).toContain('could not be confirmed');
            expect((error as NodeJS.ErrnoException).code).toBeUndefined();
            await expect(writeFileNoFollow(target, 'changed')).rejects.toThrow('could not be confirmed');
            expect(readFileSync(target, 'utf8')).toBe('content');
        } finally {
            readlink.mockRestore();
        }
    });
});
