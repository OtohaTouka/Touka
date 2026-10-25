import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
    buildDiscoveryPayload,
    MCP_ENDPOINTS,
} from '../mcp/contracts';

describe('ToukaIDE MCP 2026 contracts', () => {
    it('q36 advertises only modern Streamable HTTP', () => {
        expect(MCP_ENDPOINTS).toEqual({
            modern: '/mcp',
        });

        expect(buildDiscoveryPayload(3005, 'session-secret')).toEqual({
            ideInfo: { name: 'ToukaUI' },
            port: 3005,
            authToken: 'session-secret',
            transport: {
                type: 'streamable-http',
                endpoint: '/mcp',
                protocolMode: '2026-07-28',
            },
        });
    });

    it('q36 uses the stable modern TypeScript SDK v2 packages', () => {
        const manifest = JSON.parse(
            readFileSync(resolve(process.cwd(), 'package.json'), 'utf8'),
        ) as {
            dependencies: Record<string, string>;
            devDependencies: Record<string, string>;
            scripts: Record<string, string>;
            engines: Record<string, string>;
        };

        expect(manifest.dependencies['@modelcontextprotocol/sdk']).toBeUndefined();
        expect(manifest.dependencies['@modelcontextprotocol/server']).toBe('2.3.1');
        expect(manifest.dependencies['@modelcontextprotocol/node']).toBe('2.0.0');
        expect(manifest.dependencies['@modelcontextprotocol/server-legacy']).toBeUndefined();
        expect(manifest.devDependencies['@modelcontextprotocol/client']).toBe('2.3.1');
        expect(manifest.scripts.test).toBe('vitest run');
        expect(manifest.scripts.typecheck).toBe('tsc --noEmit');
        expect(manifest.engines.node).toBe('>=20');
        expect(manifest.engines.vscode).toBe('^1.90.0');
    });

    it('does not mount unauthenticated global CORS before MCP auth', () => {
        const serverSource = readFileSync(
            resolve(process.cwd(), 'src', 'mcp', 'server.ts'),
            'utf8',
        );

        expect(serverSource).not.toContain('app.use(cors())');
        expect(serverSource).not.toContain('bodyParser.json()');
    });
});
