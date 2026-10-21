export const MCP_ENDPOINTS = {
    modern: '/mcp',
} as const;

export interface IdeDiscoveryPayload {
    ideInfo: {
        name: 'ToukaUI';
    };
    port: number;
    authToken: string;
    transport: {
        type: 'streamable-http';
        endpoint: typeof MCP_ENDPOINTS.modern;
        protocolMode: '2026-07-28';
    };
}

export function buildDiscoveryPayload(
    port: number,
    authToken: string,
): IdeDiscoveryPayload {
    return {
        ideInfo: { name: 'ToukaUI' },
        port,
        authToken,
        transport: {
            type: 'streamable-http',
            endpoint: MCP_ENDPOINTS.modern,
            protocolMode: '2026-07-28',
        },
    };
}
