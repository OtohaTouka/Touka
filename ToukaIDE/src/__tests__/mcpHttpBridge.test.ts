import type { AddressInfo } from 'node:net';
import { request as httpRequest } from 'node:http';

import {
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    Client,
    PROTOCOL_VERSION_META_KEY,
    StreamableHTTPClientTransport,
} from '@modelcontextprotocol/client';
import express from 'express';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { McpHttpBridge } from '../mcp/httpBridge';
import type { IdeToolExecutor } from '../mcp/protocolServer';
import { createAuthMiddleware } from '../utils/authMiddleware';

const TOKEN = 'p4b-test-bearer';

interface RunningBridge {
    bridge: McpHttpBridge;
    httpServer: ReturnType<express.Application['listen']>;
    url: string;
}

interface TestCloseable {
    close(): Promise<void>;
}

interface BridgeModernResources {
    modernHandler: TestCloseable;
}

const running: RunningBridge[] = [];

async function startBridge(
    executeTool: IdeToolExecutor,
): Promise<RunningBridge> {
    const app = express();
    app.use(express.json());
    const bridge = new McpHttpBridge(executeTool);
    bridge.mount(app, createAuthMiddleware(TOKEN));
    const httpServer = await new Promise<ReturnType<express.Application['listen']>>(
        (resolveServer) => {
            const listening = app.listen(0, '127.0.0.1', () => resolveServer(listening));
        },
    );
    const port = (httpServer.address() as AddressInfo).port;
    const result = {
        bridge,
        httpServer,
        url: `http://127.0.0.1:${port}`,
    };
    running.push(result);
    return result;
}

async function sendHttpRequest(
    url: string,
    options: {
        method: string;
        headers?: Record<string, string>;
        body?: string;
    },
): Promise<{ body: string; status: number }> {
    return new Promise((resolveResponse, rejectResponse) => {
        const request = httpRequest(url, options, (response) => {
            const chunks: Buffer[] = [];
            response.on('data', (chunk: Buffer) => chunks.push(chunk));
            response.once('end', () => resolveResponse({
                body: Buffer.concat(chunks).toString('utf8'),
                status: response.statusCode ?? 0,
            }));
        });
        request.once('error', rejectResponse);
        request.end(options.body);
    });
}

async function sendHttpHeadersOnlyRequest(
    url: string,
    options: {
        method: string;
        headers?: Record<string, string>;
    },
): Promise<number> {
    return new Promise((resolveResponse) => {
        let settled = false;
        const finish = (status: number): void => {
            if (settled) {
                return;
            }
            settled = true;
            clearTimeout(timeout);
            resolveResponse(status);
        };
        const request = httpRequest(url, options, (response) => {
            finish(response.statusCode ?? 0);
            response.destroy();
            request.destroy();
        });
        const timeout = setTimeout(() => {
            request.destroy();
            finish(0);
        }, 100);
        request.once('error', () => finish(0));
        request.end();
    });
}

async function closeHttpServer(
    server: ReturnType<express.Application['listen']>,
): Promise<void> {
    await new Promise<void>((resolveClose, rejectClose) => {
        server.close((error) => {
            if (error) {
                rejectClose(error);
                return;
            }
            resolveClose();
        });
    });
}

function authProvider() {
    return { token: async () => TOKEN };
}

afterEach(async () => {
    for (const item of running.splice(0)) {
        await item.bridge.close();
        await closeHttpServer(item.httpServer);
    }
});

describe('McpHttpBridge', () => {
    it('q36 removes both legacy HTTP endpoints', async () => {
        const instance = await startBridge(async () => 'unreachable');
        const headers = { authorization: `Bearer ${TOKEN}` };
        expect(await sendHttpHeadersOnlyRequest(`${instance.url}/mcp/sse`, {
            method: 'GET', headers,
        })).toBe(404);
        const messages = await sendHttpRequest(`${instance.url}/mcp/messages`, {
            method: 'POST', headers,
        });
        expect(messages.status).toBe(404);
    });

    it('q36 rejects a legacy initialize on the modern endpoint', async () => {
        const instance = await startBridge(async () => 'unreachable');
        const response = await sendHttpRequest(`${instance.url}/mcp`, {
            method: 'POST',
            headers: {
                authorization: `Bearer ${TOKEN}`,
                accept: 'application/json, text/event-stream',
                'content-type': 'application/json',
            },
            body: JSON.stringify({
                jsonrpc: '2.0', id: 1, method: 'initialize',
                params: {
                    protocolVersion: '2025-11-25', capabilities: {},
                    clientInfo: { name: 'q36-old-wire', version: '1' },
                },
            }),
        });
        expect(response.status).toBeGreaterThanOrEqual(400);
        expect(JSON.parse(response.body)).not.toHaveProperty('result');
    });

    it('serves the 2026-07-28 era on the primary /mcp endpoint', async () => {
        const instance = await startBridge(async (name) => `called:${name}`);
        const client = new Client(
            { name: 'p4b-modern-test', version: '1.0.0' },
            { versionNegotiation: { mode: { pin: '2026-07-28' } } },
        );
        const transport = new StreamableHTTPClientTransport(
            new URL(`${instance.url}/mcp`),
            { authProvider: authProvider() },
        );

        try {
            await client.connect(transport);
            expect(client.getProtocolEra()).toBe('modern');
            expect((await client.listTools()).tools.map((tool) => tool.name))
                .toContain('get_open_file');
            const result = await client.callTool({
                name: 'get_open_file',
                arguments: {},
            });
            expect(result.content).toEqual([
                { type: 'text', text: 'called:get_open_file' },
            ]);
        } finally {
            await client.close();
        }
    });

    it('rejects missing and invalid Bearer credentials before protocol handling', async () => {
        const instance = await startBridge(async () => 'unreachable');
        const request = {
            method: 'POST',
            headers: {
                'content-type': 'application/json',
                accept: 'application/json, text/event-stream',
            },
            body: JSON.stringify({
                jsonrpc: '2.0',
                id: 1,
                method: 'server/discover',
                params: {},
            }),
        };

        const missing = await fetch(`${instance.url}/mcp`, request);
        expect(missing.status).toBe(401);

        const invalid = await fetch(`${instance.url}/mcp`, {
            ...request,
            headers: {
                ...request.headers,
                authorization: 'Bearer wrong-token',
            },
        });
        expect(invalid.status).toBe(401);
    });

    it('rejects path aliases, wrong methods, and removed legacy endpoints', async () => {
        const instance = await startBridge(async () => 'unreachable');
        const body = JSON.stringify({
            jsonrpc: '2.0',
            id: 1,
            method: 'server/discover',
            params: {},
        });
        const validHeaders = {
            accept: 'application/json, text/event-stream',
            authorization: `Bearer ${TOKEN}`,
            'content-type': 'application/json',
        };

        await sendHttpRequest(`${instance.url}/mcp`, {
            method: 'POST',
            headers: {
                ...validHeaders,
                authorization: 'Bearer invalid',
            },
            body,
        });
        await sendHttpRequest(`${instance.url}/mcp`, {
            method: 'POST',
            headers: {
                ...validHeaders,
                host: 'attacker.example',
            },
            body,
        });
        await sendHttpRequest(`${instance.url}/mcp`, {
            method: 'POST',
            headers: {
                ...validHeaders,
                origin: 'https://attacker.example',
            },
            body,
        });
        await sendHttpRequest(`${instance.url}/not-mcp`, {
            method: 'POST',
            headers: validHeaders,
            body,
        });
        await sendHttpRequest(`${instance.url}/mcp`, {
            method: 'DELETE',
            headers: validHeaders,
        });
        const modernCaseAlias = await sendHttpRequest(`${instance.url}/MCP`, {
            method: 'POST',
            headers: validHeaders,
            body,
        });
        const modernSlashAlias = await sendHttpRequest(`${instance.url}/mcp/`, {
            method: 'POST',
            headers: validHeaders,
            body,
        });
        const messagesCaseAlias = await sendHttpRequest(`${instance.url}/MCP/MESSAGES`, {
            method: 'POST',
            headers: validHeaders,
            body: '{}',
        });
        const messagesSlashAlias = await sendHttpRequest(`${instance.url}/mcp/messages/`, {
            method: 'POST',
            headers: validHeaders,
            body: '{}',
        });
        const legacyHead = await sendHttpHeadersOnlyRequest(`${instance.url}/mcp/sse`, {
            method: 'HEAD',
            headers: validHeaders,
        });

        expect([
            modernCaseAlias.status,
            modernSlashAlias.status,
            messagesCaseAlias.status,
            messagesSlashAlias.status,
        ]).toEqual([404, 404, 404, 404]);
        expect(legacyHead).toBe(404);
    });

    it('rejects late guarded requests after close begins',
        async () => {
            const instance = await startBridge(async () => 'unreachable');

            const closeResult = instance.bridge.close();
            const lateResponse = await sendHttpRequest(
                `${instance.url}/mcp`,
                {
                    method: 'POST',
                    headers: {
                        authorization: `Bearer ${TOKEN}`,
                        'content-type': 'application/json',
                    },
                    body: '{}',
                },
            );
            await closeResult;

            expect(lateResponse).toEqual({
                body: 'MCP bridge is shutting down.',
                status: 503,
            });
        });

    it.each(['reject', 'throw', 'stall'] as const)(
        'bounds modern cleanup failure %s without leaking details',
        async (failure) => {
            vi.useFakeTimers();
            const warningMessages: string[] = [];
            const warningSpy = vi.spyOn(process, 'emitWarning').mockImplementation(
                (warning) => {
                    warningMessages.push(String(warning));
                },
            );
            const bridge = new McpHttpBridge(async () => 'unreachable');
            const resources = bridge as unknown as BridgeModernResources;
            const close = vi.fn(() => {
                if (failure === 'throw') {
                    throw new Error('private synchronous server detail');
                }
                if (failure === 'reject') {
                    return Promise.reject(new Error('private server path C:\\Users\\owner\\secret'));
                }
                return new Promise<void>(() => undefined);
            });
            resources.modernHandler = { close };

            try {
                const closeResult = bridge.close();
                const closeExpectation = expect(closeResult).resolves.toBeUndefined();
                await vi.runAllTimersAsync();
                await closeExpectation;

                expect(close).toHaveBeenCalledOnce();
                expect(warningMessages).toEqual([
                    'ToukaIDE MCP bridge cleanup degraded.',
                ]);
                expect(warningMessages.join(' ')).not.toContain('C:\\Users');
                expect(warningMessages.join(' ')).not.toContain('D:\\sensitive');
            } finally {
                warningSpy.mockRestore();
                vi.useRealTimers();
            }
        });

    it('returns 404 for removed legacy messages', async () => {
        const instance = await startBridge(async () => 'unreachable');

        const response = await sendHttpRequest(`${instance.url}/mcp/messages`, {
            method: 'POST',
            headers: {
                authorization: `Bearer ${TOKEN}`,
                'content-type': 'application/json',
            },
            body: '{}',
        });

        expect(response.status).toBe(404);
    });

    it('isolates concurrent modern clients', async () => {
        const instance = await startBridge(async (_name, args) => String(args.uri));
        const makeClient = () => {
            const client = new Client(
                { name: 'p4b-multi-client', version: '1.0.0' },
                { versionNegotiation: { mode: 'auto' } },
            );
            const transport = new StreamableHTTPClientTransport(
                new URL(`${instance.url}/mcp`),
                { authProvider: authProvider() },
            );
            return { client, transport };
        };
        const first = makeClient();
        const second = makeClient();

        try {
            await Promise.all([
                first.client.connect(first.transport),
                second.client.connect(second.transport),
            ]);
            const [firstResult, secondResult] = await Promise.all([
                first.client.callTool({
                    name: 'get_diagnostics',
                    arguments: { uri: 'client-a' },
                }),
                second.client.callTool({
                    name: 'get_diagnostics',
                    arguments: { uri: 'client-b' },
                }),
            ]);
            expect(firstResult.content).toEqual([{ type: 'text', text: 'client-a' }]);
            expect(secondResult.content).toEqual([{ type: 'text', text: 'client-b' }]);
        } finally {
            await Promise.all([first.client.close(), second.client.close()]);
        }
    });

    it('propagates modern request cancellation while another client survives', async () => {
        let markStarted: (() => void) | undefined;
        const started = new Promise<void>((resolveStarted) => {
            markStarted = resolveStarted;
        });
        let cancelled = false;
        const instance = await startBridge(
            async (_name, args, signal) => {
                if (args.command === 'survivor') {
                    return 'survived';
                }
                return new Promise<string>((_resolve, reject) => {
                    markStarted?.();
                    signal.addEventListener('abort', () => {
                        cancelled = true;
                        reject(new Error('cancelled'));
                    }, { once: true });
                });
            },
        );
        const survivor = new Client(
            { name: 'q36-survivor', version: '1.0.0' },
            { versionNegotiation: { mode: { pin: '2026-07-28' } } },
        );
        await survivor.connect(new StreamableHTTPClientTransport(
            new URL(`${instance.url}/mcp`), { authProvider: authProvider() },
        ));
        try {
            const responseOpened = new Promise<import('node:http').IncomingMessage>(
                (resolveResponse, rejectResponse) => {
                    const request = httpRequest(
                        `${instance.url}/mcp`,
                        {
                            method: 'POST',
                            headers: {
                                authorization: `Bearer ${TOKEN}`,
                                accept: 'application/json, text/event-stream',
                                'content-type': 'application/json',
                                'mcp-protocol-version': '2026-07-28',
                                'mcp-method': 'tools/call',
                                'mcp-name': 'execute_terminal',
                            },
                        },
                        resolveResponse,
                    );
                    request.once('error', rejectResponse);
                    request.end(JSON.stringify({
                        jsonrpc: '2.0',
                        id: 7,
                        method: 'tools/call',
                        params: {
                            _meta: {
                                [PROTOCOL_VERSION_META_KEY]: '2026-07-28',
                                [CLIENT_INFO_META_KEY]: {
                                    name: 'p4b-cancel-test',
                                    version: '1.0.0',
                                },
                                [CLIENT_CAPABILITIES_META_KEY]: {},
                            },
                            name: 'execute_terminal',
                            arguments: { command: 'npm test' },
                        },
                    }));
                },
            );

            const response = await responseOpened;
            expect(response.statusCode).toBe(200);
            expect(response.headers['content-type']).toContain('text/event-stream');
            await started;
            response.socket.destroy();
            for (let attempt = 0; attempt < 20 && !cancelled; attempt += 1) {
                await new Promise((resolveTick) => setTimeout(resolveTick, 10));
            }
            expect(cancelled).toBe(true);
            const result = await survivor.callTool({
                name: 'execute_terminal', arguments: { command: 'survivor' },
            });
            expect(result.content).toEqual([{ type: 'text', text: 'survived' }]);
        } finally {
            await survivor.close();
        }
    });
});
