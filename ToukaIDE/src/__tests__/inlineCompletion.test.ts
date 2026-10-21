import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 行內補全的模型不寫死：設定留空＝不啟用、不發任何請求（ADR-0250 A2）。
const settings = new Map<string, unknown>();

vi.mock('vscode', () => {
    class Position {
        constructor(public line: number, public character: number) {}
    }
    class Range {
        constructor(public start: unknown, public end: unknown) {}
    }
    class InlineCompletionItem {
        constructor(public insertText: string, public range: unknown) {}
    }
    return {
        Position,
        Range,
        InlineCompletionItem,
        workspace: {
            getConfiguration: () => ({
                get: <T>(key: string, fallback?: T): T | undefined =>
                    settings.has(key) ? (settings.get(key) as T) : fallback,
            }),
        },
    };
});

const { mockFetchTouka } = vi.hoisted(() => ({ mockFetchTouka: vi.fn() }));
vi.mock('../utils/api', () => ({ fetchTouka: mockFetchTouka }));

import { ToukaInlineCompletionProvider } from '../providers/InlineCompletionProvider';

const TEXT = 'function add(a, b) {\n    return ';

function fakeDocument() {
    return {
        offsetAt: (pos: { character: number }) => pos.character,
        positionAt: (offset: number) => ({ line: 0, character: Math.min(Math.max(offset, 0), TEXT.length) }),
        getText: (range: { start: { character: number }; end: { character: number } }) =>
            TEXT.slice(range.start.character, range.end.character),
    };
}

/** VS Code CancellationToken 的替身：cancel() 觸發 onCancellationRequested 的監聽者。 */
function fakeToken() {
    const listeners: (() => void)[] = [];
    const dispose = vi.fn();
    return {
        isCancellationRequested: false,
        onCancellationRequested: (listener: () => void) => {
            listeners.push(listener);
            return { dispose };
        },
        cancel() {
            this.isCancellationRequested = true;
            listeners.forEach((listener) => listener());
        },
        dispose,
    };
}

/** 送出請求並跑過 350ms 防抖；回傳還沒 await 的結果。 */
async function startCompletion(token = fakeToken()) {
    const provider = new ToukaInlineCompletionProvider();
    const pending = provider.provideInlineCompletionItems(
        fakeDocument() as never,
        { line: 0, character: TEXT.length } as never,
        {} as never,
        token as never,
    );
    await vi.advanceTimersByTimeAsync(400);
    return pending;
}

async function requestCompletion() {
    return startCompletion();
}

/** fetch 一直等，直到收到的 signal 中止才以它的原因拒絕（模擬中止與逾時）。 */
function hangUntilAborted() {
    mockFetchTouka.mockImplementation((_path: string, init: RequestInit) => new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(init.signal?.reason));
    }));
}

describe('ToukaIDE 行內補全的模型設定', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        settings.clear();
        mockFetchTouka.mockReset();
        mockFetchTouka.mockResolvedValue({
            ok: true,
            json: async () => ({ choices: [{ message: { content: 'a + b;' } }] }),
        });
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it('沒有設定模型時（用預設值）不發任何請求', async () => {
        await expect(requestCompletion()).resolves.toBeNull();
        expect(mockFetchTouka).not.toHaveBeenCalled();
    });

    it('模型設成空字串時不發任何請求', async () => {
        settings.set('inlineCompletionModel', '');
        await expect(requestCompletion()).resolves.toBeNull();
        expect(mockFetchTouka).not.toHaveBeenCalled();
    });

    it('有設模型時用設定的模型送出', async () => {
        settings.set('inlineCompletionModel', 'my-fast-model');
        const items = await requestCompletion();
        expect(mockFetchTouka).toHaveBeenCalledTimes(1);
        const [, init] = mockFetchTouka.mock.calls[0];
        expect(JSON.parse(init.body as string).model).toBe('my-fast-model');
        expect(items).toHaveLength(1);
    });

    it('送出的請求不帶取樣參數（後端一律不送，ADR-0241）', async () => {
        settings.set('inlineCompletionModel', 'my-fast-model');
        await requestCompletion();
        const [, init] = mockFetchTouka.mock.calls[0];
        const body = JSON.parse(init.body as string);
        for (const name of ['temperature', 'top_p', 'top_k']) {
            expect(body).not.toHaveProperty(name);
        }
    });

    it('package.json 的預設值是空字串，說明寫明留空表示不啟用', () => {
        const pkg = JSON.parse(readFileSync(resolve(__dirname, '../../package.json'), 'utf-8'));
        const setting = pkg.contributes.configuration.properties['touka.inlineCompletionModel'];
        expect(setting.default).toBe('');
        expect(setting.description).toContain('留空表示不啟用');
    });
});

describe('ToukaIDE 行內補全的用途標頭、逾時與取消（r55）', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        settings.clear();
        settings.set('inlineCompletionModel', 'my-fast-model');
        mockFetchTouka.mockReset();
        mockFetchTouka.mockResolvedValue({
            ok: true,
            json: async () => ({ choices: [{ message: { content: 'a + b;' } }] }),
        });
    });

    afterEach(() => {
        vi.restoreAllMocks();
        vi.useRealTimers();
    });

    it('送出時帶 X-Touka-Purpose: inline-completion，完成後退訂取消通知', async () => {
        const token = fakeToken();
        await expect(startCompletion(token)).resolves.toHaveLength(1);
        const [path, init] = mockFetchTouka.mock.calls[0];
        expect(path).toBe('/v1/chat/completions');
        expect(init.headers).toEqual({ 'X-Touka-Purpose': 'inline-completion' });
        expect(token.dispose).toHaveBeenCalled();
    });

    it('逾時設 10 秒，和取消合併成同一個 signal', async () => {
        const timeout = vi.spyOn(AbortSignal, 'timeout');
        await startCompletion();
        expect(timeout).toHaveBeenCalledWith(10_000);
        const [, init] = mockFetchTouka.mock.calls[0];
        expect(init.signal).toBeInstanceOf(AbortSignal);
        expect(init.signal.aborted).toBe(false);
    });

    it('VS Code 取消時中止送出中的請求，結果是 null，不印錯誤', async () => {
        const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
        hangUntilAborted();
        const token = fakeToken();
        const pending = startCompletion(token);
        await vi.advanceTimersByTimeAsync(0);
        await vi.waitFor(() => expect(mockFetchTouka).toHaveBeenCalledTimes(1));
        token.cancel();
        await expect(pending).resolves.toBeNull();
        const [, init] = mockFetchTouka.mock.calls[0];
        expect(init.signal.aborted).toBe(true);
        expect(error).not.toHaveBeenCalled();
    });

    it('防抖期間就取消：不送出，結果是 null', async () => {
        const provider = new ToukaInlineCompletionProvider();
        const token = fakeToken();
        const pending = provider.provideInlineCompletionItems(
            fakeDocument() as never,
            { line: 0, character: TEXT.length } as never,
            {} as never,
            token as never,
        );
        token.cancel();
        await vi.advanceTimersByTimeAsync(400);
        await expect(pending).resolves.toBeNull();
        expect(mockFetchTouka).not.toHaveBeenCalled();
    });

    it('被新請求取代的舊請求，在 VS Code 取消後也會結束（null）', async () => {
        const provider = new ToukaInlineCompletionProvider();
        const call = (token: ReturnType<typeof fakeToken>) => provider.provideInlineCompletionItems(
            fakeDocument() as never, { line: 0, character: TEXT.length } as never, {} as never,
            token as never,
        );
        const firstToken = fakeToken();
        const first = call(firstToken);
        let firstSettled = false;
        void first.then(() => { firstSettled = true; });
        const second = call(fakeToken());  // 清掉第一個的防抖計時器
        firstToken.cancel();
        await vi.advanceTimersByTimeAsync(400);
        expect(firstSettled).toBe(true);
        await expect(first).resolves.toBeNull();
        await expect(second).resolves.toHaveLength(1);
        expect(mockFetchTouka).toHaveBeenCalledTimes(1);
    });

    it('10 秒逾時：結果是 null，不印錯誤', async () => {
        const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
        const timer = new AbortController();
        vi.spyOn(AbortSignal, 'timeout').mockReturnValue(timer.signal);
        hangUntilAborted();
        const pending = startCompletion();
        await vi.advanceTimersByTimeAsync(0);
        await vi.waitFor(() => expect(mockFetchTouka).toHaveBeenCalledTimes(1));
        timer.abort(new DOMException('The operation timed out.', 'TimeoutError'));
        await expect(pending).resolves.toBeNull();
        expect(error).not.toHaveBeenCalled();
    });

    it('其他錯誤照舊印出', async () => {
        const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
        mockFetchTouka.mockRejectedValue(new TypeError('fetch failed'));
        await expect(startCompletion()).resolves.toBeNull();
        expect(error).toHaveBeenCalledTimes(1);
    });
});
