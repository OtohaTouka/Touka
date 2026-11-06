import {
    chmodSync,
    existsSync,
    lstatSync,
    mkdirSync,
    mkdtempSync,
    readFileSync,
    rmSync,
    statSync,
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

// 只為了模擬「另一個視窗剛好先建了檔案」：其餘呼叫都交給真的 openSync
const { openSync, realFs } = vi.hoisted(() => ({
    openSync: vi.fn(),
    realFs: {} as { openSync?: typeof import('node:fs').openSync },
}));
vi.mock('node:fs', async (importOriginal) => {
    const actual = await importOriginal<typeof import('node:fs')>();
    realFs.openSync = actual.openSync;
    openSync.mockImplementation(actual.openSync);
    return { ...actual, default: { ...actual, openSync }, openSync };
});

import {
    isSafeTempRoot,
    privateDirVerdict,
    writeDiscoveryFile,
} from '../mcp/discoveryFile';

const DIR = 0o040000;
const FILE = 0o100000;
const LINK = 0o120000;
const ME = 1000;
const OTHER = 1001;

function stat(mode: number, uid: number) {
    return {
        mode,
        uid,
        isDirectory: () => (mode & 0o170000) === DIR,
        isSymbolicLink: () => (mode & 0o170000) === LINK,
    };
}

describe('discovery file trust rules (ADR-0294)', () => {
    it('accepts a sticky temp folder owned by root or the user', () => {
        expect(isSafeTempRoot(stat(DIR | 0o1777, 0), ME)).toBe(true);
        expect(isSafeTempRoot(stat(DIR | 0o1777, ME), ME)).toBe(true);
    });

    it('accepts a temp folder that only the user can write', () => {
        expect(isSafeTempRoot(stat(DIR | 0o700, ME), ME)).toBe(true);
        expect(isSafeTempRoot(stat(DIR | 0o755, ME), ME)).toBe(true);
    });

    it('rejects a temp folder others can write without the sticky bit', () => {
        expect(isSafeTempRoot(stat(DIR | 0o777, 0), ME)).toBe(false);
        expect(isSafeTempRoot(stat(DIR | 0o775, ME), ME)).toBe(false);
    });

    it('rejects a sticky temp folder owned by someone else', () => {
        expect(isSafeTempRoot(stat(DIR | 0o1777, OTHER), ME)).toBe(false);
    });

    it('rejects a temp path that is not a folder', () => {
        expect(isSafeTempRoot(stat(FILE | 0o600, ME), ME)).toBe(false);
    });

    it('refuses a folder that is a link, not a folder, or someone else\'s', () => {
        expect(privateDirVerdict(stat(LINK | 0o777, ME), ME)).toBe('link');
        expect(privateDirVerdict(stat(FILE | 0o600, ME), ME)).toBe('not-dir');
        expect(privateDirVerdict(stat(DIR | 0o700, OTHER), ME)).toBe('not-owner');
    });

    it('asks to tighten the user\'s own folder that others can write', () => {
        expect(privateDirVerdict(stat(DIR | 0o775, ME), ME)).toBe('tighten');
        expect(privateDirVerdict(stat(DIR | 0o757, ME), ME)).toBe('tighten');
    });

    it('accepts the user\'s own folder that only the user can write', () => {
        expect(privateDirVerdict(stat(DIR | 0o700, ME), ME)).toBe('ok');
        expect(privateDirVerdict(stat(DIR | 0o755, ME), ME)).toBe('ok');
    });
});

// 實際建檔、權限位元與連結只在 POSIX 判斷得出；Windows 的暫存區每人一份，照舊寫（GitHub CI 在 Linux 跑這段）
describe.skipIf(process.platform === 'win32')('writeDiscoveryFile on POSIX (ADR-0294)', () => {
    let temp: string;
    let outside: string;

    beforeEach(() => {
        temp = mkdtempSync(join(tmpdir(), 'toukaide-discovery-'));
        outside = mkdtempSync(join(tmpdir(), 'toukaide-discovery-out-'));
        writeFileSync(join(outside, 'secret.txt'), 'original');
    });

    afterEach(() => {
        rmSync(temp, { recursive: true, force: true });
        rmSync(outside, { recursive: true, force: true });
        openSync.mockImplementation(realFs.openSync!);
        openSync.mockClear();
    });

    it('writes the file readable only by the user in folders only the user can enter', () => {
        const file = writeDiscoveryFile(temp, 'touka.json', '{"a":1}');

        expect(file).toBe(join(temp, 'gemini', 'ide', 'touka.json'));
        expect(readFileSync(file, 'utf8')).toBe('{"a":1}');
        expect(statSync(file).mode & 0o777).toBe(0o600);
        expect(statSync(join(temp, 'gemini')).mode & 0o777).toBe(0o700);
        expect(statSync(join(temp, 'gemini', 'ide')).mode & 0o777).toBe(0o700);
    });

    it('takes write access away from others on the user\'s own folders', () => {
        mkdirSync(join(temp, 'gemini', 'ide'), { recursive: true });
        chmodSync(join(temp, 'gemini'), 0o775);
        chmodSync(join(temp, 'gemini', 'ide'), 0o777);

        writeDiscoveryFile(temp, 'touka.json', '{}');

        expect(statSync(join(temp, 'gemini')).mode & 0o777).toBe(0o755);
        expect(statSync(join(temp, 'gemini', 'ide')).mode & 0o777).toBe(0o755);
    });

    it('refuses a temp folder others can write without the sticky bit', () => {
        chmodSync(temp, 0o777);

        expect(() => writeDiscoveryFile(temp, 'touka.json', '{}')).toThrow();
        expect(existsSync(join(temp, 'gemini'))).toBe(false);
    });

    it('refuses a folder that is a link', () => {
        symlinkSync(outside, join(temp, 'gemini'));

        expect(() => writeDiscoveryFile(temp, 'touka.json', '{}')).toThrow();
        expect(existsSync(join(outside, 'ide'))).toBe(false);
    });

    it('replaces an old file that is a link without touching its target', () => {
        mkdirSync(join(temp, 'gemini', 'ide'), { recursive: true, mode: 0o700 });
        const file = join(temp, 'gemini', 'ide', 'touka.json');
        symlinkSync(join(outside, 'secret.txt'), file);

        writeDiscoveryFile(temp, 'touka.json', '{}');

        expect(readFileSync(join(outside, 'secret.txt'), 'utf8')).toBe('original');
        expect(lstatSync(file).isFile()).toBe(true);
        expect(readFileSync(file, 'utf8')).toBe('{}');
    });

    it('tries once more when another window creates the file first', () => {
        const file = join(temp, 'gemini', 'ide', 'touka.json');
        let raced = false;
        openSync.mockImplementation((target: string, flags: number, mode?: number) => {
            if (target === file && !raced) {
                raced = true;
                writeFileSync(file, 'other window');
                throw Object.assign(new Error('EEXIST'), { code: 'EEXIST' });
            }
            return realFs.openSync!(target, flags, mode);
        });

        writeDiscoveryFile(temp, 'touka.json', '{}');

        expect(readFileSync(file, 'utf8')).toBe('{}');
        expect(statSync(file).mode & 0o777).toBe(0o600);
    });

    it('gives up when the file keeps appearing', () => {
        const file = join(temp, 'gemini', 'ide', 'touka.json');
        openSync.mockImplementation((target: string, flags: number, mode?: number) => {
            if (target === file) {
                throw Object.assign(new Error('EEXIST'), { code: 'EEXIST' });
            }
            return realFs.openSync!(target, flags, mode);
        });

        expect(() => writeDiscoveryFile(temp, 'touka.json', '{}')).toThrow();
        expect(openSync.mock.calls.filter(([target]) => target === file)).toHaveLength(2);
    });
});
