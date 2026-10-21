/**
 * ADR-0246 U2c-7: the IDE skips its own confirmation only for a valid
 * human-approval marker that reached it over the loopback + bearer /mcp route.
 * Real ToukaMcpServer + real bridge; VS Code and process spawning are mocked.
 */
import { EventEmitter } from 'node:events';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { request as httpRequest } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    Client,
    PROTOCOL_VERSION_META_KEY,
    StreamableHTTPClientTransport,
} from '@modelcontextprotocol/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
    showInformationMessage: vi.fn(),
    executeCommand: vi.fn(),
    sendText: vi.fn(),
    spawn: vi.fn(),
    commit: vi.fn(),
    roots: [] as string[],
}));

vi.mock('vscode', () => {
    const terminal = { sendText: mocks.sendText, show: vi.fn() };
    return {
        window: {
            setStatusBarMessage: vi.fn(),
            showInformationMessage: mocks.showInformationMessage,
            activeTerminal: terminal,
            createTerminal: vi.fn(() => terminal),
            activeTextEditor: undefined,
            tabGroups: { all: [] },
        },
        workspace: {
            get workspaceFolders() {
                return mocks.roots.length > 0
                    ? mocks.roots.map((fsPath) => ({ uri: { fsPath } }))
                    : undefined;
            },
        },
        commands: { executeCommand: mocks.executeCommand },
        Uri: { file: (fsPath: string) => ({ fsPath }) },
        extensions: {
            getExtension: () => ({
                exports: { getAPI: () => ({ repositories: [{ commit: mocks.commit }] }) },
            }),
        },
    };
});
vi.mock('node:child_process', () => ({ spawn: mocks.spawn }));

import { AuthLevel } from '../mcp/authPolicy';
import { ToukaMcpServer } from '../mcp/server';

// Same key/time as humanApproval.test.ts; MACs computed outside both products.
const KEY = 'u2c7-session-bearer-0123456789abcdef';
const NOW = 1_760_000_000_000;
const EXP = 1_760_000_060_000;
const META_KEY = 'touka.dev/human-approval';
const TERMINAL_ARGS = {
    command: 'npm test -- --grep "桃花"\n',
    agent_id: 'Alice',
    z: [1, { b: true, a: null }],
};
const TERMINAL_MAC = 'db039dec16396074c01a7ebca5906c2d4f711defd678aa78aacde6b9e92da4df';

function marker(nonce: string, mac: string): Record<string, unknown> {
    return { [META_KEY]: { v: 1, nonce, exp: EXP, mac } };
}

const TERMINAL_MARKER = marker('00112233445566778899aabbccddeeff', TERMINAL_MAC);

let server: ToukaMcpServer | undefined;
let workspace: string | undefined;

function fakeProcess(): EventEmitter {
    const child = Object.assign(new EventEmitter(), {
        stdout: new EventEmitter(),
        stderr: new EventEmitter(),
        kill: vi.fn(),
    });
    queueMicrotask(() => child.emit('close', 0));
    return child;
}

function modalCalls(): unknown[][] {
    return mocks.showInformationMessage.mock.calls.filter(
        (call) => (call[1] as { modal?: boolean } | undefined)?.modal === true,
    );
}

async function startServer(level: AuthLevel = AuthLevel.Manual): Promise<number> {
    server = new ToukaMcpServer();
    server.setAuthLevel(level);
    await server.start(0, KEY);
    const listener = (server as unknown as { server: { address(): AddressInfo } }).server;
    return listener.address().port;
}

async function callTool(
    port: number,
    name: string,
    args: Record<string, unknown>,
    meta?: Record<string, unknown>,
): Promise<string> {
    const client = new Client(
        { name: 'u2c7-approval-test', version: '1.0.0' },
        { versionNegotiation: { mode: { pin: '2026-07-28' } } },
    );
    await client.connect(new StreamableHTTPClientTransport(
        new URL(`http://127.0.0.1:${port}/mcp`),
        { authProvider: { token: async () => KEY } },
    ));
    try {
        const result = await client.callTool({
            name,
            arguments: args,
            ...(meta ? { _meta: meta } : {}),
        });
        return (result.content as { text: string }[])[0].text;
    } finally {
        await client.close();
    }
}

function rawToolCall(port: number, headers: Record<string, string>): Promise<number> {
    const body = JSON.stringify({
        jsonrpc: '2.0',
        id: 9,
        method: 'tools/call',
        params: {
            _meta: {
                [PROTOCOL_VERSION_META_KEY]: '2026-07-28',
                [CLIENT_INFO_META_KEY]: { name: 'u2c7-raw', version: '1.0.0' },
                [CLIENT_CAPABILITIES_META_KEY]: {},
                ...TERMINAL_MARKER,
            },
            name: 'execute_terminal',
            arguments: TERMINAL_ARGS,
        },
    });
    return new Promise((resolveStatus, rejectStatus) => {
        const request = httpRequest(`http://127.0.0.1:${port}/mcp`, {
            method: 'POST',
            headers: {
                accept: 'application/json, text/event-stream',
                'content-type': 'application/json',
                'mcp-protocol-version': '2026-07-28',
                'mcp-method': 'tools/call',
                'mcp-name': 'execute_terminal',
                ...headers,
            },
        }, (response) => {
            response.resume();
            resolveStatus(response.statusCode ?? 0);
        });
        request.once('error', rejectStatus);
        request.end(body);
    });
}

beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    mocks.showInformationMessage.mockResolvedValue('允許');
    mocks.spawn.mockImplementation(fakeProcess);
});

afterEach(async () => {
    await server?.stop();
    server = undefined;
    vi.useRealTimers();
    vi.clearAllMocks();
    mocks.roots = [];
    if (workspace) {
        rmSync(workspace, { recursive: true, force: true });
        workspace = undefined;
    }
});

describe('ToukaIDE human-approved calls (ADR-0246 U2c-7)', () => {
    it('asks in Manual when the call carries no marker', async () => {
        const port = await startServer();
        await callTool(port, 'execute_terminal', TERMINAL_ARGS);
        expect(modalCalls()).toHaveLength(1);
        expect(mocks.spawn).toHaveBeenCalledTimes(1);
    });

    it('does not ask again for a valid marker and runs the command once', async () => {
        const port = await startServer();
        const text = await callTool(port, 'execute_terminal', TERMINAL_ARGS, TERMINAL_MARKER);
        expect(text).toContain('[EXIT_CODE: 0]');
        expect(modalCalls()).toHaveLength(0);
        expect(mocks.spawn).toHaveBeenCalledTimes(1);
        expect(mocks.sendText).not.toHaveBeenCalled();
    });

    it('asks for a forged marker or a marker minted for another call', async () => {
        const port = await startServer();
        const forged = marker('00112233445566778899aabbccddeeff', `f${TERMINAL_MAC.slice(1)}`);
        await callTool(port, 'execute_terminal', TERMINAL_ARGS, forged);
        await callTool(port, 'execute_terminal', { ...TERMINAL_ARGS, command: 'npm publish' },
            TERMINAL_MARKER);
        expect(modalCalls()).toHaveLength(2);
    });

    it('asks again when a valid marker is replayed', async () => {
        const port = await startServer();
        await callTool(port, 'execute_terminal', TERMINAL_ARGS, TERMINAL_MARKER);
        expect(modalCalls()).toHaveLength(0);
        await callTool(port, 'execute_terminal', TERMINAL_ARGS, TERMINAL_MARKER);
        expect(modalCalls()).toHaveLength(1);
    });

    it('ignores a marker that does not arrive over loopback with the bearer', async () => {
        const port = await startServer();
        expect(await rawToolCall(port, {})).toBe(401);
        expect(await rawToolCall(port, { authorization: 'Bearer wrong-token' })).toBe(401);
        expect(await rawToolCall(port, {
            authorization: `Bearer ${KEY}`,
            host: 'attacker.example',
        })).toBe(403);
        expect(mocks.spawn).not.toHaveBeenCalled();
        expect(modalCalls()).toHaveLength(0);
    });

    it('still denies a blacklisted command with a valid marker', async () => {
        const port = await startServer(AuthLevel.YOLO);
        const text = await callTool(port, 'execute_terminal', { command: 'git push origin main' },
            marker('11'.repeat(16), '28ac4c8fe40b3287f247ad5e054deae8f1e5a050680a24d12d326a92c6cfa458'));
        expect(text).toContain('git push origin main');
        expect(mocks.spawn).not.toHaveBeenCalled();
        expect(modalCalls()).toHaveLength(0);
    });

    it('commits once without asking for a valid marker, asks without one', async () => {
        const port = await startServer();
        const args = { message: 'feat: approved' };
        await callTool(port, 'git_commit', args,
            marker('33'.repeat(16), '4b1f4fd58d39c9af6796262951dc02328ebae743b55741ee77c27ed37293d6ba'));
        expect(modalCalls()).toHaveLength(0);
        expect(mocks.commit).toHaveBeenCalledTimes(1);
        await callTool(port, 'git_commit', args);
        expect(modalCalls()).toHaveLength(1);
    });

    it('writes directly for a valid marker, proposes a diff without one', async () => {
        workspace = mkdtempSync(join(tmpdir(), 'u2c7-ide-'));
        mocks.roots = [workspace];
        const port = await startServer();
        const args = { file_path: 'approved.txt', content: 'hello\n' };
        await callTool(port, 'write_file', args,
            marker('22'.repeat(16), '1967805d12824563ae9a6be415b1e4686e325a40e01fb3cddf33b8dbb7c2c5d7'));
        expect(readFileSync(join(workspace, 'approved.txt'), 'utf8')).toBe('hello\n');
        expect(mocks.executeCommand).not.toHaveBeenCalled();
        await callTool(port, 'write_file', { file_path: 'approved.txt', content: 'other\n' });
        expect(mocks.executeCommand.mock.calls[0][0]).toBe('vscode.diff');
        expect(readFileSync(join(workspace, 'approved.txt'), 'utf8')).toBe('hello\n');
    });

    it('runs YOLO calls without a marker or a modal', async () => {
        const port = await startServer(AuthLevel.YOLO);
        await callTool(port, 'execute_terminal', { command: 'npm install' });
        expect(modalCalls()).toHaveLength(0);
        expect(mocks.spawn).toHaveBeenCalledTimes(1);
    });
});
