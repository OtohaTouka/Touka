import * as vscode from 'vscode';
import { fetchTouka } from '../utils/api';
import { CompletionCache } from '../utils/CompletionCache';

// r55：用途標頭只讓 Proxy 對 Claude 5.5 少想；逾時與取消都是預期的結果、不印錯誤
const PURPOSE_HEADER = 'X-Touka-Purpose';
const INLINE_COMPLETION_PURPOSE = 'inline-completion';
const INLINE_COMPLETION_TIMEOUT_MS = 10_000;
const EXPECTED_ABORTS = new Set(['AbortError', 'TimeoutError']);

export class ToukaInlineCompletionProvider implements vscode.InlineCompletionItemProvider {
    private timeout: NodeJS.Timeout | undefined;
    private readonly cache = new CompletionCache(20);
    private abortController: AbortController | undefined;

    public async provideInlineCompletionItems(
        document: vscode.TextDocument,
        position: vscode.Position,
        context: vscode.InlineCompletionContext,
        token: vscode.CancellationToken
    ): Promise<vscode.InlineCompletionItem[] | vscode.InlineCompletionList | null | undefined> {

        const config = vscode.workspace.getConfiguration('touka');
        if (!config.get<boolean>('enableInlineCompletion', true)) {
            return null;
        }
        // 模型不寫死：沒設模型就不補全、不發請求（ADR-0250）
        const model = config.get<string>('inlineCompletionModel', '').trim();
        if (!model) {
            return null;
        }

        // 1. 擷取上下文 (FIM - Fill In the Middle)
        // 游標前 2000 字元，游標後 500 字元
        const offset = document.offsetAt(position);
        const prefix = document.getText(new vscode.Range(
            document.positionAt(Math.max(0, offset - 2000)),
            position
        ));
        const suffix = document.getText(new vscode.Range(
            position,
            document.positionAt(offset + 500)
        ));

        // 檢查快取（P2-1 fix: CompletionCache is now true LRU）
        const cacheKey = `${prefix}|${suffix}`;
        const cachedResult = this.cache.get(cacheKey);
        if (cachedResult) {
            return [new vscode.InlineCompletionItem(cachedResult, new vscode.Range(position, position))];
        }

        // 快速過濾：如果只有幾個字元且非重要觸發點，暫不補全
        if (prefix.trim().length < 3) { return null; }

        // 2. Debounce 機制 (350ms)
        return new Promise((resolve) => {
            if (this.timeout) { clearTimeout(this.timeout); }
            if (this.abortController) { this.abortController.abort(); }
            const controller = new AbortController();
            this.abortController = controller;
            // VS Code 取消（游標移動、新的請求）：中止送出中的請求並結束這個 Promise
            const cancellation = token.onCancellationRequested(() => {
                controller.abort();
                resolve(null);
            });

            this.timeout = setTimeout(async () => {
                if (token.isCancellationRequested) {
                    cancellation.dispose();
                    resolve(null);
                    return;
                }

                try {
                    // 呼叫 ToukaProxy 進行補全
                    const response = await fetchTouka('/v1/chat/completions', {
                        method: 'POST',
                        headers: { [PURPOSE_HEADER]: INLINE_COMPLETION_PURPOSE },
                        body: JSON.stringify({
                            model: model,
                            messages: [
                                {
                                    role: 'system',
                                    content: 'You are a code completion engine. Your task is to provide the EXACT code that fits between the given PREFIX and SUFFIX. Reply ONLY with the code to be inserted. No markdown blocks, no explanations.'
                                },
                                {
                                    role: 'user',
                                    content: `<|FIM_PREFIX|>${prefix}<|FIM_SUFFIX|>${suffix}\n\nInsert code at cursor:`
                                }
                            ],
                            max_tokens: 128,
                            stop: ["<|FIM_PREFIX|>", "<|FIM_SUFFIX|>", "```"]
                        }),
                        signal: AbortSignal.any([
                            controller.signal,
                            AbortSignal.timeout(INLINE_COMPLETION_TIMEOUT_MS),
                        ])
                    } as RequestInit);

                    if (response.ok) {
                        const data = await response.json() as { choices?: { message?: { content?: string } }[] };
                        let completionText = data.choices?.[0]?.message?.content || '';

                        // 3. 後處理：清理內容
                        completionText = this._cleanCompletionText(completionText);

                        if (completionText) {
                            this.cache.set(cacheKey, completionText);
                            const item = new vscode.InlineCompletionItem(completionText, new vscode.Range(position, position));
                            resolve([item]);
                            return;
                        }
                    }
                } catch (error: unknown) {
                    if (error instanceof Error && !EXPECTED_ABORTS.has(error.name)) {
                        console.error('[ToukaIDE] Inline completion failed:', error);
                    }
                } finally {
                    cancellation.dispose();
                }

                resolve(null);
            }, 350);
        });
    }

    private _cleanCompletionText(text: string): string {
        let cleaned = text;
        // 移除 Markdown 標籤
        if (cleaned.includes('```')) {
            cleaned = cleaned.replace(/```[a-z]*\n/g, '').replace(/```/g, '');
        }
        // 移除 AI 碎碎唸或引導文字
        cleaned = cleaned.replace(/^(Here is the code:|Result:)/i, '');

        return cleaned.trimEnd();
    }
}
