import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import type express from 'express';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('vscode', () => ({
    window: {
        setStatusBarMessage: vi.fn(),
        showInformationMessage: vi.fn(),
    },
}));

import { ToukaMcpServer } from '../mcp/server';

const TOKEN = 'p11b-lifecycle-token';

interface HarnessOptions {
    bridgeCloseError?: Error;
    deferListenerClose?: boolean;
    listenerCloseError?: Error;
}

class ControlledListener extends EventEmitter {
    public listening = false;
    private closeCallback: ((error?: Error) => void) | undefined;
    private onListening: (() => void) | undefined;

    public constructor(
        private readonly events: string[],
        private readonly closeError?: Error,
        private readonly deferClose: boolean = false,
    ) {
        super();
        this.on('error', () => undefined);
    }

    public captureListeningCallback(callback: () => void): void {
        this.onListening = callback;
    }

    public signalListening(): void {
        this.listening = true;
        this.onListening?.();
    }

    public signalBindError(error: Error): void {
        this.emit('error', error);
    }

    public close(callback?: (error?: Error) => void): this {
        this.events.push('listener:close');
        this.listening = false;
        if (this.deferClose) {
            this.closeCallback = callback;
        } else {
            queueMicrotask(() => callback?.(this.closeError));
        }
        return this;
    }

    public completeClose(): void {
        const callback = this.closeCallback;
        this.closeCallback = undefined;
        callback?.(this.closeError);
    }
}

interface LifecycleHarness {
    app: express.Application;
    bridge: {
        close(): Promise<void>;
        mount(app: express.Application, authMiddleware: express.RequestHandler): void;
    };
    events: string[];
    listener: ControlledListener;
}

function createHarness(options: HarnessOptions = {}): LifecycleHarness {
    const events: string[] = [];
    const listener = new ControlledListener(
        events,
        options.listenerCloseError,
        options.deferListenerClose,
    );
    const app = {
        listen: vi.fn((_port: number, _host: string, onListening: () => void) => {
            events.push('listener:listen');
            listener.captureListeningCallback(onListening);
            return listener;
        }),
        use: vi.fn(),
    } as unknown as express.Application;
    const bridge = {
        mount: vi.fn(() => {
            events.push('bridge:mount');
        }),
        close: vi.fn(async () => {
            events.push('bridge:close');
            if (options.bridgeCloseError) {
                throw options.bridgeCloseError;
            }
        }),
    };
    return { app, bridge, events, listener };
}

function createServer(harness: LifecycleHarness): ToukaMcpServer {
    return new ToukaMcpServer({
        app: harness.app,
        httpBridge: harness.bridge,
    });
}

const servers: ToukaMcpServer[] = [];

afterEach(async () => {
    for (const server of servers.splice(0)) {
        await server.stop();
    }
});

describe('ToukaMcpServer lifecycle', () => {
    it('awaits listener readiness before discovery and awaits shutdown on deactivate', () => {
        const extensionSource = readFileSync(
            resolve(process.cwd(), 'src', 'extension.ts'),
            'utf8',
        );
        const start = extensionSource.indexOf('await mcpServer.start(');
        const discovery = extensionSource.indexOf('writeDiscoveryFile(MCP_PORT');

        expect(extensionSource).toContain('export async function activate(');
        expect(start).toBeGreaterThan(-1);
        expect(discovery).toBeGreaterThan(start);
        expect(extensionSource).toContain('export async function deactivate(');
        expect(extensionSource).toContain('await mcpServer.stop()');
    });

    it('waits for listener readiness before resolving start', async () => {
        const harness = createHarness();
        const server = createServer(harness);
        servers.push(server);

        const startResult: unknown = server.start(0, TOKEN);
        expect(startResult).toBeInstanceOf(Promise);
        expect(harness.events).toEqual(['bridge:mount', 'listener:listen']);

        harness.listener.signalListening();
        await startResult;

        expect(harness.events).toEqual([
            'bridge:mount',
            'listener:listen',
        ]);
    });

    it('rejects start when the listener fails to bind', async () => {
        const harness = createHarness();
        const server = createServer(harness);
        servers.push(server);

        const startResult: unknown = server.start(0, TOKEN);
        expect(startResult).toBeInstanceOf(Promise);
        if (!(startResult instanceof Promise)) {
            return;
        }
        harness.listener.signalBindError(new Error('address already in use'));

        await expect(startResult).rejects.toThrow('address already in use');
    });

    it('cancels a pending start and still closes the bridge', async () => {
        const harness = createHarness({
            listenerCloseError: Object.assign(
                new Error('Server is not running.'),
                { code: 'ERR_SERVER_NOT_RUNNING' },
            ),
        });
        const server = createServer(harness);

        const startResult = server.start(0, TOKEN);
        const stopResult = server.stop();

        await expect(startResult).rejects.toThrow('start cancelled');
        await expect(stopResult).resolves.toBeUndefined();
        expect(harness.listener.listenerCount('error')).toBe(2);
        expect(harness.events).toEqual([
            'bridge:mount',
            'listener:listen',
            'listener:close',
            'bridge:close',
        ]);
    });

    it('does not let a delayed listener close block bridge cleanup', async () => {
        const harness = createHarness({ deferListenerClose: true });
        const server = createServer(harness);
        const startResult = server.start(0, TOKEN);
        harness.listener.signalListening();
        await startResult;

        const stopResult = server.stop();
        await vi.waitFor(() => {
            expect(harness.bridge.close).toHaveBeenCalledOnce();
        });
        expect(harness.events.slice(-2)).toEqual([
            'listener:close',
            'bridge:close',
        ]);

        harness.listener.completeClose();
        await expect(stopResult).resolves.toBeUndefined();
    });

    it('bounds a missing listener close callback', async () => {
        const harness = createHarness({ deferListenerClose: true });
        const server = createServer(harness);
        const startResult = server.start(0, TOKEN);
        harness.listener.signalListening();
        await startResult;
        vi.useFakeTimers();

        try {
            const stopResult = server.stop();
            const stopExpectation = expect(stopResult).rejects.toThrow(
                'Touka MCP Server listener shutdown timed out.',
            );
            await vi.runAllTimersAsync();
            await stopExpectation;

            expect(harness.bridge.close).toHaveBeenCalledOnce();
            expect(harness.events.slice(-2)).toEqual([
                'listener:close',
                'bridge:close',
            ]);
        } finally {
            vi.useRealTimers();
        }
    });

    it('makes concurrent start and stop calls idempotent', async () => {
        const harness = createHarness();
        const server = createServer(harness);
        servers.push(server);

        const firstStart: unknown = server.start(0, TOKEN);
        expect(firstStart).toBeInstanceOf(Promise);
        if (!(firstStart instanceof Promise)) {
            return;
        }
        const secondStart = server.start(0, TOKEN);
        expect(secondStart).toBe(firstStart);
        harness.listener.signalListening();
        await Promise.all([firstStart, secondStart]);

        const firstStop = server.stop();
        const secondStop = server.stop();
        expect(secondStop).toBe(firstStop);
        await Promise.all([firstStop, secondStop]);

        expect(harness.app.listen).toHaveBeenCalledTimes(1);
        expect(harness.bridge.close).toHaveBeenCalledTimes(1);
    });

    it('attempts every cleanup when earlier cleanup fails', async () => {
        const harness = createHarness({
            bridgeCloseError: new Error('bridge close failed'),
            listenerCloseError: new Error('listener close failed'),
        });
        const server = createServer(harness);

        const startResult: unknown = server.start(0, TOKEN);
        expect(startResult).toBeInstanceOf(Promise);
        if (!(startResult instanceof Promise)) {
            await server.stop();
            return;
        }
        harness.listener.signalListening();
        await startResult;

        await expect(server.stop()).rejects.toBeDefined();

        expect(harness.events.slice(-2)).toEqual([
            'listener:close',
            'bridge:close',
        ]);
    });
});
