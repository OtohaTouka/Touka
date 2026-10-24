import type { AddressInfo } from 'node:net';
import { request as httpRequest } from 'node:http';
import zlib, { brotliCompressSync, deflateSync, gzipSync } from 'node:zlib';

import express from 'express';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('vscode', () => ({
    window: {
        setStatusBarMessage: vi.fn(),
        showInformationMessage: vi.fn(),
    },
}));

import { ToukaMcpServer } from '../mcp/server';

const PAYLOAD = JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' });

interface RunningApp {
    httpServer: ReturnType<express.Application['listen']>;
    reachedRoute: () => number;
    url: string;
}

const running: RunningApp[] = [];

// The server's own JSON middleware runs before this probe route, as it does
// before the bridge's Bearer check in production.
async function startServerApp(): Promise<RunningApp> {
    const app = express();
    new ToukaMcpServer({
        app,
        httpBridge: { mount: vi.fn(), close: vi.fn(async () => undefined) },
    });
    let routeHits = 0;
    app.post('/probe', (request, response) => {
        routeHits += 1;
        response.json({ body: request.body ?? null });
    });
    const httpServer = await new Promise<ReturnType<express.Application['listen']>>(
        (resolveServer) => {
            const listening = app.listen(0, '127.0.0.1', () => resolveServer(listening));
        },
    );
    const port = (httpServer.address() as AddressInfo).port;
    const result = {
        httpServer,
        reachedRoute: () => routeHits,
        url: `http://127.0.0.1:${port}/probe`,
    };
    running.push(result);
    return result;
}

async function post(
    url: string,
    body: Buffer,
    headers: Record<string, string> = {},
): Promise<{ body: string; status: number }> {
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
                status: response.statusCode ?? 0,
            }));
        });
        request.once('error', rejectResponse);
        request.end(body);
    });
}

// body-parser looks these up on the shared zlib module when it decompresses.
function spyOnDecompressors(): Array<ReturnType<typeof vi.spyOn>> {
    return [
        vi.spyOn(zlib, 'createGunzip'),
        vi.spyOn(zlib, 'createInflate'),
        vi.spyOn(zlib, 'createBrotliDecompress'),
    ];
}

afterEach(async () => {
    vi.restoreAllMocks();
    for (const { httpServer } of running.splice(0)) {
        await new Promise<void>((resolveClose) => httpServer.close(() => resolveClose()));
    }
});

describe('ToukaMcpServer JSON body parsing', () => {
    it('parses an uncompressed JSON body', async () => {
        const app = await startServerApp();

        const response = await post(app.url, Buffer.from(PAYLOAD));

        expect(response.status).toBe(200);
        expect(JSON.parse(response.body)).toEqual({ body: JSON.parse(PAYLOAD) });
    });

    it('treats Content-Encoding identity as uncompressed', async () => {
        const app = await startServerApp();

        const response = await post(app.url, Buffer.from(PAYLOAD), {
            'Content-Encoding': 'identity',
        });

        expect(response.status).toBe(200);
        expect(JSON.parse(response.body)).toEqual({ body: JSON.parse(PAYLOAD) });
    });

    it.each([
        ['gzip', gzipSync],
        ['deflate', deflateSync],
        ['br', brotliCompressSync],
    ])('rejects a %s-encoded body with 415 without decompressing it', async (encoding, compress) => {
        const app = await startServerApp();
        const decompressors = spyOnDecompressors();

        const response = await post(app.url, compress(Buffer.from(PAYLOAD)), {
            'Content-Encoding': encoding,
        });

        expect(response.status).toBe(415);
        expect(app.reachedRoute()).toBe(0);
        for (const decompressor of decompressors) {
            expect(decompressor).not.toHaveBeenCalled();
        }
    });

    // GHSA-27r3-jxmh-p828 describes a compressed frame followed by trailing
    // bytes. This checks that request shape is refused before decompression;
    // it does not reproduce the crash.
    it('refuses a gzip frame with trailing bytes before decompressing it', async () => {
        const app = await startServerApp();
        const decompressors = spyOnDecompressors();
        const crafted = Buffer.concat([
            gzipSync(Buffer.from(PAYLOAD)),
            Buffer.from('trailing bytes after the gzip frame'),
        ]);

        const rejected = await post(app.url, crafted, { 'Content-Encoding': 'gzip' });
        const next = await post(app.url, Buffer.from(PAYLOAD));

        expect(rejected.status).toBe(415);
        expect(next.status).toBe(200);
        expect(app.reachedRoute()).toBe(1);
        for (const decompressor of decompressors) {
            expect(decompressor).not.toHaveBeenCalled();
        }
    });
});
