import * as fs from 'node:fs';

/**
 * 讀寫工作區裡的檔案，不跟隨連結（ADR-0294）。
 *
 * 傳進來的路徑必須是 `resolveWorkspacePath` 解析後的真實位置（Linux 拿它和實際開到的位置比對）；這之後到開檔之間，路上可能被換成連結。
 * POSIX 用 `O_NOFOLLOW` 開檔（最後一段是連結就打不開），加 `O_NONBLOCK`（FIFO 不會卡住），開好後只讀寫一般檔案。
 * Linux 開好後再從 `/proc/self/fd` 看這個檔案實際在哪裡，和要求的路徑不同（中間的資料夾被換成連結）就不讀寫；
 * 寫入不在開檔時清空，確認之後才清空再寫。
 * Windows 沒有 `O_NOFOLLOW`：讀寫之前確認最後一段不是連結，檢查到讀寫之間仍有很短的時間差；
 * macOS 沒有 `/proc`，中間資料夾的時間差仍在。見 ADR-0294「已知的界線」。
 */
type Verb = 'read' | 'write';

function refusal(verb: Verb, reason: string): Error {
    return new Error(`Refusing to ${verb} ${reason}`);
}

async function refuseLinkBeforeUse(target: string, verb: Verb): Promise<void> {
    const info = await fs.promises.lstat(target).catch((error: NodeJS.ErrnoException) => {
        if (error.code === 'ENOENT') {
            return undefined;
        }
        throw error;
    });
    if (info?.isSymbolicLink()) {
        throw refusal(verb, 'through a link');
    }
    if (info && !info.isFile()) {
        throw refusal(verb, 'a path that is not a regular file');
    }
}

async function openNoFollow(target: string, verb: Verb, flags: number): Promise<fs.promises.FileHandle> {
    const { O_NOFOLLOW, O_NONBLOCK } = fs.constants;
    let handle: fs.promises.FileHandle;
    try {
        handle = await fs.promises.open(target, flags | O_NOFOLLOW | O_NONBLOCK, 0o666);
    } catch (error: unknown) {
        // Linux／macOS 回 ELOOP，FreeBSD 回 EMLINK
        const code = (error as NodeJS.ErrnoException).code;
        if (code === 'ELOOP' || code === 'EMLINK') {
            throw refusal(verb, 'through a link');
        }
        throw error;
    }
    try {
        if (!(await handle.stat()).isFile()) {
            throw refusal(verb, 'a path that is not a regular file');
        }
        if (process.platform === 'linux') {
            // 讀不到 /proc（例如沒掛 /proc 的沙盒）也拒絕，而且不沿用 ENOENT，免得被當成檔案不存在
            const actual = await fs.promises.readlink(`/proc/self/fd/${handle.fd}`).catch(() => undefined);
            if (actual === undefined) {
                throw refusal(verb, 'because the location of the opened file could not be confirmed');
            }
            if (actual !== target) {
                throw refusal(verb, 'because the path changed while opening the file');
            }
        }
        return handle;
    } catch (error: unknown) {
        await handle.close();
        throw error;
    }
}

export async function readFileNoFollow(target: string): Promise<string> {
    if (fs.constants.O_NOFOLLOW === undefined) {
        await refuseLinkBeforeUse(target, 'read');
        return fs.promises.readFile(target, 'utf8');
    }
    const handle = await openNoFollow(target, 'read', fs.constants.O_RDONLY);
    try {
        return await handle.readFile('utf8');
    } finally {
        await handle.close();
    }
}

export async function writeFileNoFollow(target: string, content: string): Promise<void> {
    if (fs.constants.O_NOFOLLOW === undefined) {
        await refuseLinkBeforeUse(target, 'write');
        await fs.promises.writeFile(target, content, 'utf8');
        return;
    }
    const handle = await openNoFollow(target, 'write', fs.constants.O_WRONLY | fs.constants.O_CREAT);
    try {
        await handle.truncate(0);
        await handle.writeFile(content, 'utf8');
    } finally {
        await handle.close();
    }
}
