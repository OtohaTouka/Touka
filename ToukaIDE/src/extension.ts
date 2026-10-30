import * as vscode from 'vscode';
import * as fs from 'fs';
import * as os from 'os';
import { writeDiscoveryFile } from './mcp/discoveryFile';
import { ToukaMcpServer, AuthLevel } from './mcp/server';
import { ToukaCodeLensProvider } from './providers/CodeLensProvider';
import { ToukaInlineCompletionProvider } from './providers/InlineCompletionProvider';
import { ToukaChatViewProvider } from './providers/ChatViewProvider';
import { handleInlineChat } from './commands/inlineChat';
import { contextTracker } from './utils/ContextTracker';
import { terminalOutputTracker } from './utils/TerminalOutputTracker';
import { generateAuthToken } from './utils/authToken';
import { buildDiscoveryPayload } from './mcp/contracts';

// --- [Discovery SSOT Constants] ---
const MCP_PORT = 3005;
const DISCOVERY_FILENAME = 'gemini-ide-server-touka.json';

let mcpServer: ToukaMcpServer | undefined;
let chatProvider: ToukaChatViewProvider | undefined;
let statusBarItem: vscode.StatusBarItem;
let discoveryFilePath: string | undefined;

/**
 * Writes the IDE discovery file so that ToukaProxy can locate this server.
 * Discovery format follows the Gemini CLI Companion specification.
 * Includes the authToken, so only the user may read it (ADR-0294).
 */
function announceDiscoveryFile(port: number, authToken: string): void {
    try {
        const payload = buildDiscoveryPayload(port, authToken);
        discoveryFilePath = writeDiscoveryFile(os.tmpdir(), DISCOVERY_FILENAME, JSON.stringify(payload, null, 2));
        console.log(`[ToukaIDE] Discovery file written: ${discoveryFilePath}`);
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error('[ToukaIDE] Failed to write discovery file:', msg);
        vscode.window.showWarningMessage(`Touka 找不到這個 IDE：連線資訊檔沒有寫入（${msg}）`);
    }
}

/**
 * Removes the IDE discovery file on deactivation.
 */
function removeDiscoveryFile(): void {
    if (discoveryFilePath) {
        try {
            if (fs.existsSync(discoveryFilePath)) {
                fs.unlinkSync(discoveryFilePath);
                console.log('[ToukaIDE] Discovery file removed.');
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            console.error('[ToukaIDE] Failed to remove discovery file:', msg);
        }
    }
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
    console.log('ToukaIDE is now active!');

    // Initialize and start Context Tracking
    contextTracker.track();
    context.subscriptions.push(contextTracker);

    // Initialize terminal output tracking (for @terminal mentions)
    terminalOutputTracker.track();
    context.subscriptions.push(terminalOutputTracker);

    // P3-2: generate a session-unique auth token so only ToukaProxy can call MCP tools
    const mcpAuthToken = generateAuthToken();

    // Start the MCP Server
    mcpServer = new ToukaMcpServer();
    try {
        await mcpServer.start(MCP_PORT, mcpAuthToken);
        // Announce presence to ToukaProxy via discovery file (includes authToken for P3-2)
        announceDiscoveryFile(MCP_PORT, mcpAuthToken);
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error('[ToukaIDE] Failed to start MCP Server:', err);
        vscode.window.showErrorMessage(`Touka MCP Server 啟動失敗: ${msg}`);
    }

    chatProvider = new ToukaChatViewProvider(context.extensionUri);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(ToukaChatViewProvider.viewType, chatProvider)
    );

    // 串接 MCP 計畫更新到 Webview
    mcpServer.onPlanUpdate = (data) => {
        if (chatProvider) {
            chatProvider.postMessageToWebview(data);
        }
    };

    let disposable = vscode.commands.registerCommand('toukaide.start', () => {
        vscode.commands.executeCommand('workbench.view.extension.touka-sidebar');
    });
    context.subscriptions.push(disposable);

    // --- [分級授權機制 UI] ---
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'toukaide.setAuthLevel';
    statusBarItem.tooltip = "點擊切換 Touka 授權等級";
    updateStatusBar(AuthLevel.Manual);
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);

    let authDisposable = vscode.commands.registerCommand('toukaide.setAuthLevel', async () => {
        const items: vscode.QuickPickItem[] = [
            { label: "🛡️ Manual", description: "手動確認每一項寫入與執行 (預設)", detail: AuthLevel.Manual },
            { label: "🚀 YOLO", description: "全自動執行，黑名單指令一律拒絕 (Git/Docker)", detail: AuthLevel.YOLO },
        ];

        const selection = await vscode.window.showQuickPick(items, {
            placeHolder: "選擇 Touka 代理人的授權等級"
        });

        if (selection && mcpServer) {
            const level = selection.detail as AuthLevel;
            mcpServer.setAuthLevel(level);
            updateStatusBar(level);
        }
    });
    context.subscriptions.push(authDisposable);

    vscode.commands.registerCommand('toukaide.getAuthLevel', () => {
        return mcpServer?.getAuthLevel() || AuthLevel.Manual;
    });

    // Legacy toggle support (switches to YOLO)
    let yoloDisposable = vscode.commands.registerCommand('toukaide.toggleYolo', () => {
        if (mcpServer) {
            mcpServer.setAuthLevel(AuthLevel.YOLO);
            updateStatusBar(AuthLevel.YOLO);
        }
    });
    context.subscriptions.push(yoloDisposable);

    // --- [配置變更監聽] ---
    context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(e => {
        if (e.affectsConfiguration('touka.proxyUrl') || e.affectsConfiguration('touka.apiProtocol') || e.affectsConfiguration('touka.apiKey')) {
            vscode.window.showInformationMessage('Touka 配置已更新，正在同步連線資訊...');
            if (chatProvider) {
                // 通知 Webview 重新整理
                chatProvider.postMessageToWebview({ type: 'config_updated' });
            }
        }
    }));

    // Phase 5: Editor UX - Code Lens
    const codeLensProvider = new ToukaCodeLensProvider();
    context.subscriptions.push(
        vscode.languages.registerCodeLensProvider({ scheme: 'file' }, codeLensProvider)
    );

    context.subscriptions.push(vscode.commands.registerCommand('toukaide.action.explain', (uri: vscode.Uri, range: vscode.Range, symbolName: string) => {
        vscode.commands.executeCommand('workbench.view.extension.touka-sidebar');
        if (chatProvider) {
            chatProvider.sendMessageToWebview(`請解釋這個函式/類別：\`${symbolName}\``);
        }
    }));

    context.subscriptions.push(vscode.commands.registerCommand('toukaide.action.refactor', (uri: vscode.Uri, range: vscode.Range, symbolName: string) => {
        vscode.commands.executeCommand('workbench.view.extension.touka-sidebar');
        if (chatProvider) {
            chatProvider.sendMessageToWebview(`請幫我重構這個函式/類別：\`${symbolName}\`，讓它更簡潔且符合 SOLID 原則。`);
        }
    }));

    // Phase 5: Editor UX - Inline Completion
    const inlineCompletionProvider = new ToukaInlineCompletionProvider();
    context.subscriptions.push(
        vscode.languages.registerInlineCompletionItemProvider({ scheme: 'file' }, inlineCompletionProvider)
    );

    // Phase 5: Editor UX - Inline Chat
    context.subscriptions.push(vscode.commands.registerCommand('toukaide.inlineChat', handleInlineChat));
}

function updateStatusBar(level: AuthLevel) {
    if (!statusBarItem) { return; }
    const icon = level === AuthLevel.YOLO ? "$(rocket)" : "$(shield)";
    statusBarItem.text = `${icon} Touka: ${level}`;
}

export async function deactivate(): Promise<void> {
    removeDiscoveryFile();
    if (mcpServer) {
        await mcpServer.stop();
    }
}
