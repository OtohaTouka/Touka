import * as fs from 'node:fs';
import * as path from 'node:path';

/**
 * 寫 IDE 連線資訊檔（含權杖），只給自己讀（ADR-0294）。
 *
 * Linux 的暫存區大家共用。上層暫存資料夾要「有 sticky 位元」或「別人不能寫」，而且擁有者是 root 或自己：
 * 這樣別人不能改名或刪除屬於自己的 `gemini`。`gemini`、`ide` 兩層依序不存在就以 0700 建立；存在就不跟隨連結開啟，
 * 必須是自己的資料夾，別人能寫就先拿掉群組與其他人的寫入權（先收緊 `gemini` 再看 `ide`，`ide` 就在別人不能動的資料夾裡）。
 * 檔案先刪舊的，再以 0600、`O_EXCL|O_NOFOLLOW` 建新的；開兩個視窗時可能剛好被另一個先建，就再試一次。
 * 不符合就丟錯，由呼叫端提醒使用者。Windows 的暫存區每人一份，照舊寫。
 */
const STICKY = 0o1000;
const GROUP_OTHER_WRITE = 0o022;

export interface PathStat {
    mode: number;
    uid: number;
    isDirectory(): boolean;
    isSymbolicLink(): boolean;
}

export type DirVerdict = 'ok' | 'tighten' | 'link' | 'not-dir' | 'not-owner';

const REFUSALS: Record<Exclude<DirVerdict, 'ok' | 'tighten'>, string> = {
    'link': 'is a link',
    'not-dir': 'is not a folder',
    'not-owner': 'belongs to another user',
};

/** 上層暫存資料夾：擁有者是 root 或自己，而且有 sticky 位元或別人不能寫。 */
export function isSafeTempRoot(info: PathStat, uid: number): boolean {
    const owner = info.uid === 0 || info.uid === uid;
    const guarded = (info.mode & STICKY) !== 0 || (info.mode & GROUP_OTHER_WRITE) === 0;
    return info.isDirectory() && owner && guarded;
}

/** `gemini`、`ide` 這兩層：要是自己的資料夾；別人能寫就要先收緊。 */
export function privateDirVerdict(info: PathStat, uid: number): DirVerdict {
    if (info.isSymbolicLink()) {
        return 'link';
    }
    if (!info.isDirectory()) {
        return 'not-dir';
    }
    if (info.uid !== uid) {
        return 'not-owner';
    }
    return (info.mode & GROUP_OTHER_WRITE) === 0 ? 'ok' : 'tighten';
}

function ensurePrivateDir(dir: string, uid: number): void {
    try {
        fs.mkdirSync(dir, { mode: 0o700 });
    } catch (error: unknown) {
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') {
            throw error;
        }
    }
    let fd: number;
    try {
        fd = fs.openSync(dir, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW);
    } catch (error: unknown) {
        const code = (error as NodeJS.ErrnoException).code;
        if (code === 'ELOOP' || code === 'ENOTDIR' || code === 'EMLINK') {
            throw new Error(`${dir} is a link or not a folder; remove it to let Touka find this IDE`);
        }
        throw error;
    }
    try {
        let info = fs.fstatSync(fd);
        let verdict = privateDirVerdict(info, uid);
        if (verdict === 'tighten') {
            fs.fchmodSync(fd, info.mode & 0o7777 & ~GROUP_OTHER_WRITE);
            info = fs.fstatSync(fd);
            verdict = privateDirVerdict(info, uid);
        }
        if (verdict !== 'ok') {
            const reason = verdict === 'tighten' ? 'can still be written by others' : REFUSALS[verdict];
            throw new Error(`${dir} ${reason}; remove it to let Touka find this IDE`);
        }
    } finally {
        fs.closeSync(fd);
    }
}

function removeIfPresent(file: string): void {
    try {
        fs.unlinkSync(file);
    } catch (error: unknown) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
            throw error;
        }
    }
}

function openPrivateFile(file: string): number {
    const flags = fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW;
    removeIfPresent(file);
    try {
        return fs.openSync(file, flags, 0o600);
    } catch (error: unknown) {
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') {
            throw error;
        }
    }
    // 另一個視窗剛好先建了同名的檔案：再刪一次、再試一次
    removeIfPresent(file);
    return fs.openSync(file, flags, 0o600);
}

function writePrivateFile(file: string, contents: string): void {
    const fd = openPrivateFile(file);
    try {
        fs.writeFileSync(fd, contents);
    } finally {
        fs.closeSync(fd);
    }
}

/** 寫好後回傳檔案路徑；`uid` 是 undefined（Windows）時照舊寫。 */
export function writeDiscoveryFile(
    tempDir: string,
    fileName: string,
    contents: string,
    uid: number | undefined = process.getuid?.(),
): string {
    if (uid === undefined) {
        const ideDir = path.join(tempDir, 'gemini', 'ide');
        fs.mkdirSync(ideDir, { recursive: true });
        const file = path.join(ideDir, fileName);
        fs.writeFileSync(file, contents, 'utf8');
        return file;
    }

    const root = fs.realpathSync(tempDir);
    if (!isSafeTempRoot(fs.statSync(root), uid)) {
        throw new Error(`${root} can be changed by other users; set TMPDIR to a folder only you can write`);
    }
    let dir = root;
    for (const name of ['gemini', 'ide']) {
        dir = path.join(dir, name);
        ensurePrivateDir(dir, uid);
    }
    const file = path.join(dir, fileName);
    writePrivateFile(file, contents);
    return file;
}
