import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

import { fetchTouka } from '../utils/api';
import * as EditorHandlers from '../mcp/handlers/EditorHandlers';
import * as GitHandlers from '../mcp/handlers/GitHandlers';
import { terminalOutputTracker } from '../utils/TerminalOutputTracker';

export class ToukaChatViewProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'touka.chatView';
    private _view?: vscode.WebviewView;
    private _currentModel: string = '';
    private _availableAgents: Set<string> = new Set(); // 由 GET /souls 動態填入（見 _handleFetchAgents），不寫死 agent id

    constructor(private readonly _extensionUri: vscode.Uri) {}

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ) {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(this._extensionUri, 'dist', 'webview')]
        };
        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

        webviewView.webview.onDidReceiveMessage(async (message) => {
            switch (message.type) {
                case 'chat':
                    if (message.model) {
                        this._currentModel = message.model;
                    }
                    await this._handleChatMessage(message.content, message.session_id, webviewView.webview);
                    break;
                case 'fetch_agents':
                    await this._handleFetchAgents(webviewView.webview);
                    break;
                case 'fetch_sessions':
                    if (message.agent) {
                        this._currentModel = message.agent;
                    }
                    await this._handleFetchSessions(this._currentModel, webviewView.webview);
                    break;
                case 'load_session':
                    await this._handleLoadSession(message.session_id, webviewView.webview);
                    break;
                case 'touka.applyArtifact':
                    await this._handleApplyArtifact(message);
                    break;
                case 'cancel_task':
                    await this._handleCancelTask(message.session_id);
                    break;
                case 'fetch_files':
                    await this._handleFetchFiles(webviewView.webview);
                    break;
            }
        });
    }

    private async _handleFetchFiles(webview: vscode.Webview) {
        const files = await vscode.workspace.findFiles('**/*', '**/node_modules/**', 100);
        const fileList = files.map(f => path.relative(vscode.workspace.workspaceFolders?.[0].uri.fsPath || '', f.fsPath));
        webview.postMessage({ type: 'workspace_files', files: fileList });
    }

    private async _resolveMentions(content: string): Promise<{ content: string, attachments: unknown[] }> {
        const attachments: unknown[] = [];
        const mentionRegex = /@([\w\.\-\/]+)/g;
        let match;
        const finalContent = content;

        while ((match = mentionRegex.exec(content)) !== null) {
            const mentionId = match[1];

            if (mentionId === 'problems') {
                const diag = EditorHandlers.handleGetDiagnostics();
                attachments.push({ type: 'diagnostics', content: diag });
            } else if (mentionId === 'terminal') {
                // P1-1 fix: capture actual terminal output instead of hardcoded string
                const terminalOutput = terminalOutputTracker.getLatestOutput();
                attachments.push({ type: 'terminal', content: terminalOutput });
            } else if (mentionId === 'git') {
                const changes = await GitHandlers.handleGetGitChanges();
                attachments.push({ type: 'git', content: changes });
            } else if (mentionId === 'opened') {
                const openFiles = vscode.workspace.textDocuments.map(d => d.fileName).join('\n');
                attachments.push({ type: 'opened_files', content: openFiles });
            } else {
                try {
                    const files = await vscode.workspace.findFiles(`**/${mentionId}`);
                    if (files.length > 0) {
                        const fileContent = await fs.promises.readFile(files[0].fsPath, 'utf8');
                        attachments.push({
                            type: 'file',
                            name: mentionId,
                            content: fileContent.substring(0, 50000)
                        });
                    }
                } catch (e) {}
            }
        }

        return { content: finalContent, attachments };
    }

    private async _handleCancelTask(sessionId: string) {
        try {
            if (!sessionId) { return; }
            const response = await fetchTouka(`/ui/sessions/${sessionId}/stop`, {
                method: 'POST'
            });
            if (response.ok) {
                vscode.window.showInformationMessage(`[Touka] 已發送任務終止請求。`);
            }
        } catch (error) {
            console.error(`[ToukaIDE] Error stopping task:`, error);
        }
    }

    private async _handleFetchAgents(webview: vscode.Webview) {
        try {
            const soulResponse = await fetchTouka(`/souls`, { method: 'GET' });
            let agents: unknown[] = [];
            if (soulResponse.ok) {
                const data = await soulResponse.json() as { souls: { id: string }[] };
                agents = data.souls.map((s) => ({ id: s.id, name: s.id }));
                this._availableAgents.clear();
                data.souls.forEach((s) => this._availableAgents.add(s.id));
            }

            const modelResponse = await fetchTouka(`/models`, { method: 'GET' });
            if (modelResponse.ok) {
                const data = await modelResponse.json() as { data: { id: string; name?: string; owned_by: string }[] };
                const toukaModels = data.data.filter((m) => m.owned_by === 'touka' || m.id === 'ToukaGroup');
                toukaModels.forEach((m) => {
                    if (!(agents as { id: string }[]).find((a) => m.id === a.id || m.id === `ToukaSoul${a.id}`)) {
                        (agents as { id: string; name: string }[]).push({ id: m.id, name: m.name || m.id });
                    }
                });
            }
            // 預設模型不寫死：若尚未選擇，採用 SSOT 回傳的第一個 agent
            if (!this._currentModel && agents.length > 0) {
                this._currentModel = (agents[0] as { id: string }).id;
            }
            webview.postMessage({ type: 'agents_data', agents });
        } catch (error) {
            console.error(`[ToukaIDE] Error fetching agents:`, error);
        }
    }

    private async _handleFetchSessions(agent: string, webview: vscode.Webview) {
        try {
            const response = await fetchTouka(`/ui/sessions?agent_id=${agent}`, { method: 'GET' });
            if (response.ok) {
                const sessions = await response.json();
                webview.postMessage({ type: 'sessions_data', sessions });
            }
        } catch (error) {
            console.error(`[ToukaIDE] Error fetching sessions:`, error);
        }
    }

    // P1-3 fix: use direct GET /ui/sessions/{session_id} instead of listing all
    private async _handleLoadSession(sessionId: string, webview: vscode.Webview) {
        try {
            const response = await fetchTouka(`/ui/sessions/${sessionId}`, { method: 'GET' });
            if (response.ok) {
                const session = await response.json() as { messages?: unknown };
                if (session?.messages) {
                    let msgs = session.messages;
                    if (typeof msgs === 'string') {
                        try { msgs = JSON.parse(msgs); } catch (e) { msgs = []; }
                    }
                    webview.postMessage({ type: 'session_history', messages: msgs });
                }
            }
        } catch (error) {
            console.error(`[ToukaIDE] Error loading session ${sessionId}:`, error);
        }
    }

    private async _handleApplyArtifact(message: { content: string; title: string; type: string }) {
        const { content, title, type } = message;
        try {
            let languageId = 'plaintext';
            if (type === 'web') {
                languageId = content.includes('import React') || content.includes('className=') ? 'typescriptreact' : 'html';
            } else if (type === 'mermaid') {
                languageId = 'mermaid';
            } else if (type === 'markdown') {
                languageId = 'markdown';
            } else if (type === 'code') {
                languageId = 'typescript';
            }

            const doc = await vscode.workspace.openTextDocument({
                content: content,
                language: languageId
            });

            await vscode.window.showTextDocument(doc, {
                preview: false,
                viewColumn: vscode.ViewColumn.One
            });

            vscode.window.showInformationMessage(`[Touka] 已將 "${title}" 套用至編輯器。`);
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            vscode.window.showErrorMessage(`[Touka] 套用 Artifact 失敗: ${msg}`);
        }
    }

    // P1-2 fix: only pre-fill the webview input; do NOT auto-send the message
    public sendMessageToWebview(content: string) {
        if (this._view) {
            this._view.webview.postMessage({ type: 'prefill', content: content });
        }
    }

    public postMessageToWebview(message: unknown) {
        if (this._view) {
            this._view.webview.postMessage(message);
        }
    }

    private _getToukaConfig() {
        const config = vscode.workspace.getConfiguration('touka');
        return {
            protocol: config.get<'openai' | 'anthropic'>('apiProtocol', 'openai'),
            defaultModel: config.get<string>('defaultModel', '')
        };
    }

    private async _handleChatMessage(content: string, sessionId: string, webview: vscode.Webview) {
        try {
            const { protocol, defaultModel } = this._getToukaConfig();

            const authLevel = await vscode.commands.executeCommand('toukaide.getAuthLevel') as string;

            const { content: processedContent, attachments } = await this._resolveMentions(content);

            const agentMentionMatch = content.match(/^@(\w+)/);
            if (agentMentionMatch) {
                const targetAgent = agentMentionMatch[1];
                if (this._availableAgents.has(targetAgent)) {
                    this._currentModel = targetAgent;
                }
            }

            const endpoint = protocol === 'anthropic' ? '/v1/messages' : '/v1/chat/completions';
            const editor = vscode.window.activeTextEditor;
            const ide_context = editor ? {
                fileName: path.basename(editor.document.fileName),
                line: editor.selection.active.line + 1
            } : null;

            const body: Record<string, unknown> = {
                model: this._currentModel || defaultModel,
                stream: true,
                ide_context: ide_context,
                attachments: attachments,
                auth_level: authLevel
            };

            if (sessionId) {
                body.session_id = sessionId;
            }

            if (protocol === 'anthropic') {
                body.messages = [{ role: 'user', content: processedContent }];
                body.max_tokens = 4096;
            } else {
                body.messages = [{ role: 'user', content: processedContent }];
            }

            const response = await fetchTouka(endpoint, {
                method: 'POST',
                body: JSON.stringify(body)
            });

            if (!response.ok) { throw new Error(`Proxy Error: ${response.status}`); }

            const returnedSessionId = response.headers.get('x-session-id');
            if (returnedSessionId && returnedSessionId !== sessionId) {
                webview.postMessage({ type: 'session_created', session_id: returnedSessionId });
            }

            const reader = response.body?.getReader();
            const decoder = new TextDecoder('utf-8');

            if (reader) {
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) { break; }
                    const chunk = decoder.decode(value, { stream: true });
                    const lines = chunk.split('\n');
                    for (const line of lines) {
                        if (line.startsWith('data: ') && !line.includes('[DONE]')) {
                            try {
                                const data = JSON.parse(line.slice(6)) as Record<string, unknown>;
                                if (data.type && ['tool_start', 'tool_result', 'error', 'touka_status', 'loop_status'].includes(data.type as string)) {
                                    webview.postMessage(data);
                                    continue;
                                }
                                // P1-4 fix: handle both OpenAI and Anthropic delta formats
                                const openaiContent = (data.choices as { delta?: { content?: string } }[] | undefined)?.[0]?.delta?.content;
                                const anthropicDelta = data as { type?: string; delta?: { type?: string; text?: string } };
                                const anthropicContent = (anthropicDelta.type === 'content_block_delta' && anthropicDelta.delta?.type === 'text_delta')
                                    ? anthropicDelta.delta.text
                                    : undefined;
                                const deltaText = openaiContent || anthropicContent || '';
                                if (deltaText) {
                                    webview.postMessage({ type: 'stream', content: deltaText });
                                }
                            } catch (e) {}
                        }
                    }
                }
                webview.postMessage({ type: 'stream_done' });
            }
        } catch (error: unknown) {
            console.error('[ToukaIDE] Chat error:', error);
            const msg = error instanceof Error ? error.message : 'Unknown error';
            webview.postMessage({ type: 'error', content: msg });
        }
    }

    private _getHtmlForWebview(webview: vscode.Webview) {
        const webviewUri = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, 'dist', 'webview'));
        void webviewUri; // suppress unused warning
        const indexPath = vscode.Uri.joinPath(this._extensionUri, 'dist', 'webview', 'index.html');
        const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, 'dist', 'webview', 'style.css'));
        const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, 'dist', 'webview', 'main.js'));

        let html = '';
        try {
            html = fs.readFileSync(indexPath.fsPath, 'utf8');
            html = html.replace(/\$\{cspSource\}/g, webview.cspSource);
            html = html.replace(/\$\{styleUri\}/g, styleUri.toString());
            html = html.replace(/\$\{scriptUri\}/g, scriptUri.toString());
        } catch (e) {
            html = `<!DOCTYPE html><html><body>Error loading webview.</body></html>`;
        }
        return html;
    }
}
