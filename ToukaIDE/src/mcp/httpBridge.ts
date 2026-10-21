import {
    createMcpHandler,
    type McpHttpHandler,
} from '@modelcontextprotocol/server';
import {
    localhostHostValidation,
    localhostOriginValidation,
    toNodeHandler,
} from '@modelcontextprotocol/node';
import { AsyncLocalStorage } from 'node:async_hooks';
import type {
    Application,
    NextFunction,
    Request,
    RequestHandler,
    Response,
} from 'express';

import { MCP_ENDPOINTS } from './contracts';
import {
    createIdeMcpServer,
    type IdeToolExecutor,
} from './protocolServer';

const BRIDGE_CLOSE_TIMEOUT_MS = 1_000;

export class McpHttpBridge {
    private readonly modernHandler: McpHttpHandler;
    private readonly modernRequestSignals = new AsyncLocalStorage<AbortSignal>();
    private mounted = false;
    private closing = false;
    private closePromise: Promise<void> | undefined;

    public constructor(
        private readonly executeTool: IdeToolExecutor,
    ) {
        this.modernHandler = createMcpHandler(
            () => createIdeMcpServer(
                this.executeTool,
                this.modernRequestSignals.getStore(),
            ),
            {
                legacy: 'reject',
                // Modern cancellation is signalled by closing the request's
                // SSE stream. Always stream so long-running IDE/terminal
                // calls are cancellable before their terminal result exists.
                responseMode: 'sse',
                keepAliveMs: 100,
            },
        );
    }

    public mount(
        app: Application,
        authMiddleware: RequestHandler,
    ): void {
        if (this.mounted) {
            return;
        }
        this.mounted = true;
        const localRequestGuard = this.createLocalRequestGuard();
        const nodeHandler = toNodeHandler(this.modernHandler);

        app.all(
            MCP_ENDPOINTS.modern,
            authMiddleware,
            localRequestGuard,
            this.createExactRouteGuard('POST', MCP_ENDPOINTS.modern),
            async (req: Request, res: Response) => {
                if (this.rejectWhileClosing(res)) {
                    return;
                }
                const controller = new AbortController();
                const abort = (): void => controller.abort();
                req.once('aborted', abort);
                res.once('close', abort);
                try {
                    await this.modernRequestSignals.run(
                        controller.signal,
                        () => nodeHandler(req, res, req.body),
                    );
                } finally {
                    req.removeListener('aborted', abort);
                    res.removeListener('close', abort);
                }
            },
        );

    }

    public close(): Promise<void> {
        if (!this.closePromise) {
            this.closing = true;
            this.closePromise = this.closeOnce();
        }
        return this.closePromise;
    }

    private async closeOnce(): Promise<void> {
        const modernResult = await this.closeBounded(
            () => this.modernHandler.close(),
        );
        if (!modernResult) {
            this.emitWarningFailOpen('ToukaIDE MCP bridge cleanup degraded.');
        }
    }

    private async closeBounded(closeResource: () => Promise<void>): Promise<boolean> {
        let timeout: ReturnType<typeof setTimeout> | undefined;
        const closeResult = Promise.resolve()
            .then(closeResource)
            .then(
                () => true,
                () => false,
            );
        const timeoutResult = new Promise<boolean>((resolveTimeout) => {
            timeout = setTimeout(
                () => resolveTimeout(false),
                BRIDGE_CLOSE_TIMEOUT_MS,
            );
        });
        const result = await Promise.race([closeResult, timeoutResult]);
        if (timeout !== undefined) {
            clearTimeout(timeout);
        }
        return result;
    }

    private rejectWhileClosing(res: Response): boolean {
        if (!this.closing) {
            return false;
        }
        res.status(503).send('MCP bridge is shutting down.');
        return true;
    }

    private createLocalRequestGuard(): RequestHandler {
        const validateHost = localhostHostValidation();
        const validateOrigin = localhostOriginValidation();
        return (req: Request, res: Response, next: NextFunction): void => {
            if (!validateHost(req, res) || !validateOrigin(req, res)) {
                return;
            }
            next();
        };
    }

    private createExactRouteGuard(
        method: string,
        path: string,
    ): RequestHandler {
        return (req: Request, res: Response, next: NextFunction): void => {
            if (req.path !== path) {
                res.sendStatus(404);
                return;
            }
            if (req.method !== method) {
                res.sendStatus(405);
                return;
            }
            next();
        };
    }

    private emitWarningFailOpen(message: string): void {
        try {
            process.emitWarning(message);
        } catch {
            // Warning emission cannot become an MCP transport dependency.
        }
    }
}
