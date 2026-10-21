/** Test-only real bridge, with memory-only tools and bounded private stdio. */
import express from 'express';
import type { Server } from 'node:http';
import type { AddressInfo, Socket } from 'node:net';
import process from 'node:process';

import { McpHttpBridge } from '../../mcp/httpBridge';
import { MCP_ENDPOINTS } from '../../mcp/contracts';
import { HumanApprovalVerifier } from '../../mcp/humanApproval';
import { createAuthMiddleware } from '../../utils/authMiddleware';

const INPUT_LIMIT = 4 * 1024;
const OUTPUT_LIMIT = 64 * 1024;
const LIFETIME_MS = 45_000;
const CLOSE_MS = 3_000;
const METHODS = ['server/discover', 'tools/list', 'tools/call', 'initialize'] as const;
const RETIRED_ENDPOINTS = ['/mcp/sse', '/mcp/messages'];

let bridge: McpHttpBridge | undefined;
let server: Server | undefined;
let starting = false;
let closing = false;
let failed = false;
let warnings = 0;
let outputBytes = 0;
let executions = 0;
let legacyRequests = 0;
let active = 0;
let cancelled = 0;
let releaseSurvivor: (() => void) | undefined;
const sockets = new Set<Socket>();
const methods: Record<string, number> = Object.fromEntries(METHODS.map((method) => [method, 0]));
const statuses: Record<string, number> = {};

function emit(value: Record<string, unknown>): void {
    const line = JSON.stringify(value) + '\n';
    outputBytes += Buffer.byteLength(line);
    if (outputBytes > OUTPUT_LIMIT) {
        failed = true;
        process.exitCode = 1;
        return;
    }
    process.stdout.write(line);
}

async function stop(): Promise<void> {
    if (closing) {
        return;
    }
    closing = true;
    clearTimeout(lifetime);
    const deadline = setTimeout(() => process.exit(1), CLOSE_MS);
    try {
        await bridge?.close();
        if (server) {
            await new Promise<void>((resolve, reject) => {
                server?.close((error) => error ? reject(error) : resolve());
                // All sockets belong to this fresh server, never another process.
                for (const socket of sockets) {
                    socket.destroy();
                }
            });
        }
        await new Promise<void>((resolve) => setImmediate(resolve));
        if (warnings !== 0 || active !== 0 || sockets.size !== 0) {
            failed = true;
        }
        emit({ event: 'closed', warnings, sockets: sockets.size, active });
    } catch {
        failed = true;
        emit({ event: 'failed' });
    } finally {
        clearTimeout(deadline);
        process.exitCode = failed ? 1 : 0;
        process.stdin.destroy();
    }
}

function fail(): void {
    failed = true;
    emit({ event: 'failed' });
    void stop();
}

async function start(token: string): Promise<void> {
    if (starting || token.length < 16 || token.length > 128) {
        fail();
        return;
    }
    starting = true;
    const app = express();
    app.use(express.json({ limit: INPUT_LIMIT }));
    app.use((request, response, next) => {
        response.once('finish', () => {
            const status = String(response.statusCode);
            statuses[status] = (statuses[status] ?? 0) + 1;
        });
        if (RETIRED_ENDPOINTS.includes(request.path)) {
            legacyRequests += 1;
        }
        const method: unknown = request.body?.method;
        if (request.path === MCP_ENDPOINTS.modern && typeof method === 'string' && METHODS.some((item) => item === method)) {
            methods[method] += 1;
        }
        next();
    });
    const approvals = new HumanApprovalVerifier(token);
    bridge = new McpHttpBridge(async (name, args, signal, meta) => {
        executions += 1;
        if (name === 'get_diagnostics' && args.uri === 'approval-probe') {
            return approvals.verify(meta, name, args) ? 'human-approved' : 'not-approved';
        }
        if (name === 'get_open_file') {
            return '桃花\n{"value":null,"flags":[false,0]}';
        }
        if (name === 'get_diagnostics' && typeof args.uri === 'string') {
            if (args.uri !== 'hold-until-cancelled' && args.uri !== 'hold-until-released') {
                return args.uri;
            }
            const request = args.uri === 'hold-until-cancelled' ? 'cancelled-client' : 'survivor';
            active += 1;
            emit({ event: 'started', request });
            return await new Promise<string>((resolve, reject) => {
                let settled = false;
                const finish = (): boolean => {
                    if (settled) {
                        return false;
                    }
                    settled = true;
                    active -= 1;
                    signal.removeEventListener('abort', onAbort);
                    if (request === 'survivor') {
                        releaseSurvivor = undefined;
                    }
                    return true;
                };
                const onAbort = (): void => {
                    if (finish()) {
                        cancelled += 1;
                        emit({ event: 'cancelled', request });
                        reject(new Error('Synthetic request cancelled'));
                    }
                };
                if (signal.aborted) {
                    onAbort();
                } else {
                    signal.addEventListener('abort', onAbort, { once: true });
                    if (request === 'survivor') {
                        releaseSurvivor = () => {
                            if (finish()) {
                                resolve('survivor-completed');
                            }
                        };
                    }
                }
            });
        }
        throw new Error('Synthetic executor does not implement this tool');
    });
    bridge.mount(app, createAuthMiddleware(token));
    server = app.listen(0, '127.0.0.1');
    server.on('connection', (socket) => {
        sockets.add(socket);
        socket.once('close', () => sockets.delete(socket));
    });
    await new Promise<void>((resolve, reject) => {
        server?.once('listening', resolve);
        server?.once('error', reject);
    });
    emit({ event: 'ready', port: (server.address() as AddressInfo).port });
}

async function handle(line: string): Promise<void> {
    const value: unknown = JSON.parse(line);
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        fail();
        return;
    }
    const message = value as Record<string, unknown>;
    if (message.command === 'start' && Object.keys(message).length === 2 && typeof message.token === 'string') {
        await start(message.token);
    } else if (message.command === 'snapshot' && Object.keys(message).length === 1 && server && !closing) {
        emit({ event: 'snapshot', methods: { ...methods }, statuses: { ...statuses }, executions,
            legacy_requests: legacyRequests, active, cancelled });
    } else if (message.command === 'stop' && Object.keys(message).length === 1) {
        await stop();
    } else if (message.command === 'release-survivor' && Object.keys(message).length === 1 && !closing && releaseSurvivor) {
        releaseSurvivor();
    } else {
        fail();
    }
}

// Do not write raw exceptions, request bodies, credentials, or diagnostic stacks.
process.on('warning', () => { warnings += 1; });
process.on('uncaughtException', fail);
process.on('unhandledRejection', fail);
const lifetime = setTimeout(fail, LIFETIME_MS);
let pending = '';
let received = 0;
let control = Promise.resolve();
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk: string) => {
    received += Buffer.byteLength(chunk);
    if (received > INPUT_LIMIT) {
        fail();
        return;
    }
    pending += chunk;
    let newline = pending.indexOf('\n');
    while (newline !== -1) {
        const line = pending.slice(0, newline);
        pending = pending.slice(newline + 1);
        control = control.then(() => handle(line)).catch(fail);
        newline = pending.indexOf('\n');
    }
});
process.stdin.once('end', () => { void stop(); });
