import type { AddressInfo } from 'node:net';
import { request as httpRequest } from 'node:http';
import { gzipSync } from 'node:zlib';

import express from 'express';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('vscode', () => ({
    window: {
        setStatusBarMessage: vi.fn(),
        showInformationMessage: vi.fn(),
    },
}));

import { ToukaMcpServer } from '../mcp/server';

const TOKEN = 'error-response-test-token';

interface RunningServer {
    server: ToukaMcpServer;
    baseUrl: string;
}

const running: ToukaMcpServer[] = [];

afterEach(async () => {
    for (const server of running.splice(0)) {
        await server.stop();
    }
});

// Starts the real server on an ephemeral loopback port. A bridge that throws
// stands in for an unexpected failure after the Bearer check.
async function startServer(withThrowingBridge = false): Promise<RunningServer> {
    const app = express();
    const listen = vi.spyOn(app, 'listen');
    const server = new ToukaMcpServer({
        app,
        ...(withThrowingBridge
            ? {
                httpBridge: {
                    mount: (mountedApp: express.Application) => {
                        mountedApp.post('/mcp', () => {
                            throw new Error('unexpected failure at D:\\secret\\path');
                        });
                    },
                    close: vi.fn(async () => undefined),
                },
            }
            : {}),
    });
    running.push(server);
    await server.start(0, TOKEN);
    const listener = listen.mock.results[0]?.value as ReturnType<express.Application['listen']>;
    const port = (listener.address() as AddressInfo).port;
    return { server, baseUrl: `http://127.0.0.1:${port}` };
}

async function post(
    url: string,
    body: Buffer,
    headers: Record<string, string> = {},
): Promise<{ body: string; contentType: string; status: number }> {
    return new Promise((resolveResponse, rejectResponse) => {
        const request = httpRequest(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': String(body.length),
                ...headers,
            },
        }, (response) => {
            const chunks: Buffer[] = [];
            response.on('data', (chunk: Buffer) => chunks.push(chunk));
            response.once('end', () => resolveResponse({
                body: Buffer.concat(chunks).toString('utf8'),
                contentType: String(response.headers['content-type'] ?? ''),
                status: response.statusCode ?? 0,
            }));
        });
        request.once('error', rejectResponse);
        request.end(body);
    });
}

function expectFixedJson(
    response: { body: string; contentType: string },
    expected: Record<string, string>,
): void {
    expect(response.contentType).toMatch(/^application\/json/);
    expect(JSON.parse(response.body)).toEqual(expected);
    expect(response.body).not.toMatch(/\bat\s|node_modules|[A-Za-z]:\\|<html|<pre/i);
}

describe('ToukaMcpServer error responses', () => {
    it('answers malformed JSON with a fixed 400 body before the Bearer check', async () => {
        const { baseUrl } = await startServer();

        const response = await post(`${baseUrl}/mcp`, Buffer.from('{"jsonrpc":'));

        expect(response.status).toBe(400);
        expectFixedJson(response, { error: 'Invalid request' });
    });

    it('answers an oversized body with a fixed 413 body', async () => {
        const { baseUrl } = await startServer();
        const oversized = Buffer.from(JSON.stringify({ padding: 'x'.repeat(200 * 1024) }));

        const response = await post(`${baseUrl}/mcp`, oversized);

        expect(response.status).toBe(413);
        expectFixedJson(response, { error: 'Invalid request' });
    });

    it('answers a compressed body with a fixed 415 body', async () => {
        const { baseUrl } = await startServer();

        const response = await post(
            `${baseUrl}/mcp`,
            gzipSync(Buffer.from('{"jsonrpc":"2.0"}')),
            { 'Content-Encoding': 'gzip' },
        );

        expect(response.status).toBe(415);
        expectFixedJson(response, { error: 'Invalid request' });
    });

    it('answers an unexpected error after the bridge with a fixed 500 body', async () => {
        const { baseUrl } = await startServer(true);

        const response = await post(`${baseUrl}/mcp`, Buffer.from('{"jsonrpc":"2.0"}'));

        expect(response.status).toBe(500);
        expectFixedJson(response, { error: 'Internal server error' });
        expect(response.body).not.toContain('secret');
    });

    it('keeps the Bearer rejection for a well-formed unauthenticated request', async () => {
        const { baseUrl } = await startServer();

        const response = await post(`${baseUrl}/mcp`, Buffer.from('{"jsonrpc":"2.0"}'));

        expect(response.status).toBe(401);
        expect(JSON.parse(response.body)).toEqual({
            error: 'Missing or invalid Authorization header',
        });
    });
});
