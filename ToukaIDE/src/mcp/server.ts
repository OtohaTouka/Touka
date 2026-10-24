import * as vscode from 'vscode';
import express from 'express';

import { contextTracker } from '../utils/ContextTracker';
import { createAuthMiddleware } from '../utils/authMiddleware';
import { AuthLevel } from './authPolicy';
import * as EditorHandlers from './handlers/EditorHandlers';
import * as FileHandlers from './handlers/FileHandlers';
import * as GitHandlers from './handlers/GitHandlers';
import * as PlanHandlers from './handlers/PlanHandlers';
import * as TerminalHandlers from './handlers/TerminalHandlers';
import { McpHttpBridge } from './httpBridge';
import { HumanApprovalVerifier } from './humanApproval';
import { requestIdeApproval } from './modalApproval';

export { AuthLevel } from './authPolicy';

interface McpHttpBridgePort {
    mount(
        app: express.Application,
        authMiddleware: express.RequestHandler,
    ): void;
    close(): Promise<void>;
}

export interface ToukaMcpServerOptions {
    app?: express.Application;
    httpBridge?: McpHttpBridgePort;
}

const LISTENER_CLOSE_TIMEOUT_MS = 1_000;

function clientErrorStatus(error: unknown): number | undefined {
    if (typeof error !== 'object' || error === null) {
        return undefined;
    }
    const { status, statusCode } = error as { status?: unknown; statusCode?: unknown };
    const candidate = status ?? statusCode;
    if (
        typeof candidate === 'number'
        && Number.isInteger(candidate)
        && candidate >= 400
        && candidate < 500
    ) {
        return candidate;
    }
    return undefined;
}

// Express's default error page carries the stack trace and local file paths,
// and body parsing errors reach it before the Bearer check.
const respondWithFixedError: express.ErrorRequestHandler = (error, _req, res, next) => {
    if (res.headersSent) {
        next(error);
        return;
    }
    const status = clientErrorStatus(error);
    if (status === undefined) {
        res.status(500).json({ error: 'Internal server error' });
        return;
    }
    res.status(status).json({ error: 'Invalid request' });
};

export class ToukaMcpServer {
    private readonly app: express.Application;
    private readonly httpBridge: McpHttpBridgePort;
    private server: ReturnType<express.Application['listen']> | undefined;
    private startPromise: Promise<void> | undefined;
    private stopPromise: Promise<void> | undefined;
    private cancelPendingStart: (() => void) | undefined;
    private pendingStartCancelled = false;
    private stopRequested = false;
    private authLevel: AuthLevel = AuthLevel.Manual;
    private humanApproval: HumanApprovalVerifier | undefined;

    public onPlanUpdate?: (data: unknown) => void;

    public constructor(options: ToukaMcpServerOptions = {}) {
        this.app = options.app ?? express();
        // Runs before the Bearer check, so compressed bodies are refused with
        // 415 before any decompression stream exists (GHSA-27r3-jxmh-p828).
        // Touka never sends them.
        this.app.use(express.json({ inflate: false }));
        this.httpBridge = options.httpBridge ?? new McpHttpBridge(
            (name, args, signal, meta) => this.executeTool(name, args, signal, meta),
        );
    }

    public setAuthLevel(level: AuthLevel): void {
        this.authLevel = level;
        const status = level === AuthLevel.YOLO
            ? '🚀 授權等級: YOLO'
            : '🛡️ 授權等級: Manual';
        vscode.window.setStatusBarMessage(status, 5000);
        this.onPlanUpdate?.({ type: 'auth_change', level });
    }

    public getAuthLevel(): AuthLevel {
        return this.authLevel;
    }

    public start(port: number = 3005, authToken: string = ''): Promise<void> {
        if (this.startPromise) {
            return this.startPromise;
        }
        if (this.stopRequested) {
            this.startPromise = Promise.reject(
                new Error('Touka MCP Server is stopping.'),
            );
            return this.startPromise;
        }
        this.humanApproval = new HumanApprovalVerifier(authToken);
        this.startPromise = this.startOnce(port, authToken);
        return this.startPromise;
    }

    public stop(): Promise<void> {
        if (!this.stopPromise) {
            this.stopRequested = true;
            if (this.cancelPendingStart) {
                this.pendingStartCancelled = true;
                this.cancelPendingStart();
            }
            this.stopPromise = this.stopOnce();
        }
        return this.stopPromise;
    }

    private startOnce(port: number, authToken: string): Promise<void> {
        return new Promise<void>((resolveStart, rejectStart) => {
            let bindingSettled = false;
            const onBindError = (error: Error): void => {
                if (bindingSettled) {
                    return;
                }
                bindingSettled = true;
                this.cancelPendingStart = undefined;
                this.server?.on('error', onRuntimeError);
                this.server = undefined;
                rejectStart(error);
            };
            const onRuntimeError = (): void => {
                this.emitWarningFailOpen('ToukaIDE MCP listener degraded.');
            };
            const onListening = (): void => {
                if (bindingSettled) {
                    return;
                }
                bindingSettled = true;
                this.cancelPendingStart = undefined;
                this.server?.removeListener('error', onBindError);
                this.server?.on('error', onRuntimeError);
                void this.completeStart(port, resolveStart, rejectStart);
            };

            this.cancelPendingStart = (): void => {
                if (bindingSettled) {
                    return;
                }
                bindingSettled = true;
                this.server?.removeListener('error', onBindError);
                this.server?.on('error', onRuntimeError);
                this.cancelPendingStart = undefined;
                rejectStart(new Error('Touka MCP Server start cancelled.'));
            };

            try {
                this.httpBridge.mount(this.app, createAuthMiddleware(authToken));
                this.app.use(respondWithFixedError);
                this.server = this.app.listen(
                    port,
                    '127.0.0.1',
                    () => queueMicrotask(onListening),
                );
                this.server.once('error', onBindError);
            } catch (error: unknown) {
                bindingSettled = true;
                this.cancelPendingStart = undefined;
                this.server = undefined;
                rejectStart(error);
            }
        });
    }

    private async stopOnce(): Promise<void> {
        const errors: unknown[] = [];
        const listener = this.server;
        this.server = undefined;
        const listenerClose = listener
            ? this.beginListenerClose(listener)
            : Promise.resolve(undefined);

        try {
            await this.httpBridge.close();
        } catch (error: unknown) {
            errors.push(error);
        }
        if (this.startPromise) {
            try {
                await this.startPromise;
            } catch {
                // A bind failure or shutdown cancellation is already reflected
                // by start(); cleanup must still continue to the final seal.
            }
        }
        const listenerError = await listenerClose;
        if (
            listenerError !== undefined
            && !(
                this.pendingStartCancelled
                && this.isServerNotRunningError(listenerError)
            )
        ) {
            errors.push(listenerError);
        }

        if (errors.length === 1) {
            throw errors[0];
        }
        if (errors.length > 1) {
            const aggregate = new Error('Touka MCP Server cleanup failed.') as Error & {
                errors: unknown[];
            };
            aggregate.errors = errors;
            throw aggregate;
        }
    }

    private async completeStart(
        port: number,
        resolveStart: () => void,
        rejectStart: (reason?: unknown) => void,
    ): Promise<void> {
        if (this.stopRequested) {
            rejectStart(new Error('Touka MCP Server start cancelled.'));
            return;
        }
        try {
            vscode.window.showInformationMessage(
                `Touka MCP Server running on port ${port}`,
            );
        } catch {
            // UI notification failure cannot invalidate a listening MCP server.
        }
        resolveStart();
    }

    private beginListenerClose(
        listener: ReturnType<express.Application['listen']>,
    ): Promise<unknown | undefined> {
        return new Promise((resolveClose) => {
            let settled = false;
            const finish = (error?: unknown): void => {
                if (settled) {
                    return;
                }
                settled = true;
                clearTimeout(timeout);
                resolveClose(error);
            };
            const timeout = setTimeout(
                () => finish(
                    new Error('Touka MCP Server listener shutdown timed out.'),
                ),
                LISTENER_CLOSE_TIMEOUT_MS,
            );
            try {
                listener.close((error?: Error) => finish(error));
            } catch (error: unknown) {
                finish(error);
            }
        });
    }

    private isServerNotRunningError(error: unknown): boolean {
        return error instanceof Error
            && 'code' in error
            && error.code === 'ERR_SERVER_NOT_RUNNING';
    }

    private emitWarningFailOpen(message: string): void {
        try {
            process.emitWarning(message);
        } catch {
            // Warning emission cannot become an MCP availability dependency.
        }
    }

    private async askUserPermission(
        title: string,
        detail: string,
        humanApproved: boolean,
    ): Promise<boolean> {
        return requestIdeApproval(
            this.authLevel,
            humanApproved,
            title,
            detail,
            async (message, options, ...items) => vscode.window.showInformationMessage(
                message,
                options,
                ...items,
            ),
        );
    }

    private async executeTool(
        name: string,
        args: Record<string, unknown>,
        signal: AbortSignal,
        meta?: Record<string, unknown>,
    ): Promise<string> {
        // Only a valid marker for exactly this call skips the IDE's own confirmation.
        const humanApproved = this.humanApproval?.verify(meta, name, args) ?? false;
        switch (name) {
            case 'get_workspace_structure':
                return FileHandlers.handleGetWorkspaceStructure(args.depth as number);
            case 'get_diagnostics':
                return EditorHandlers.handleGetDiagnostics(args.uri as string);
            case 'get_open_file':
                return EditorHandlers.handleGetOpenFile();
            case 'get_recent_files':
                return `Recently accessed files:\n${contextTracker.getRecentFiles().join('\n')}`;
            case 'get_selection':
                return EditorHandlers.handleGetSelection();
            case 'read_file':
                return FileHandlers.handleReadFile(args.file_path as string);
            case 'list_directory':
                return FileHandlers.handleListDirectory(args.dir_path as string);
            case 'write_file':
                return FileHandlers.handleWriteFile(
                    args.file_path as string,
                    args.content as string,
                    this.authLevel,
                    humanApproved,
                );
            case 'execute_terminal':
                return TerminalHandlers.handleExecuteTerminal(
                    args.command as string,
                    this.authLevel,
                    (title, detail) => this.askUserPermission(title, detail, humanApproved),
                    signal,
                );
            case 'get_git_changes':
                return GitHandlers.handleGetGitChanges();
            case 'git_commit':
                return GitHandlers.handleGitCommit(
                    args.message as string,
                    (title, detail) => this.askUserPermission(title, detail, humanApproved),
                );
            case 'create_plan':
                return PlanHandlers.handleCreatePlan(
                    args.title as string,
                    args.steps as string[],
                    (data) => this.onPlanUpdate?.(data),
                );
            case 'update_task_status':
                return PlanHandlers.handleUpdateTaskStatus(
                    args.step_index as number,
                    args.status as string,
                    args.details as string,
                    (data) => this.onPlanUpdate?.(data),
                );
            default:
                throw new Error(`Tool not found: ${name}`);
        }
    }
}
