import {
    fromJsonSchema,
    McpServer,
} from '@modelcontextprotocol/server';

import { tools } from './tools';

export type IdeToolExecutor = (
    name: string,
    args: Record<string, unknown>,
    signal: AbortSignal,
    meta?: Record<string, unknown>,
) => Promise<string>;

function combineAbortSignals(
    requestSignal: AbortSignal,
    lifecycleSignal?: AbortSignal,
): AbortSignal {
    if (!lifecycleSignal) {
        return requestSignal;
    }
    const controller = new AbortController();
    const abort = (): void => controller.abort();
    if (requestSignal.aborted || lifecycleSignal.aborted) {
        abort();
        return controller.signal;
    }
    requestSignal.addEventListener('abort', abort, { once: true });
    lifecycleSignal.addEventListener('abort', abort, { once: true });
    return controller.signal;
}

export function createIdeMcpServer(
    executeTool: IdeToolExecutor,
    lifecycleSignal?: AbortSignal,
): McpServer {
    const server = new McpServer({
        name: 'touka-vscode-mcp',
        version: '2.0.0',
    });

    for (const tool of tools) {
        server.registerTool(
            tool.name,
            {
                description: tool.description,
                inputSchema: fromJsonSchema<Record<string, unknown>>(tool.inputSchema),
            },
            async (args, context) => {
                const signal = combineAbortSignals(
                    context.mcpReq.signal,
                    lifecycleSignal,
                );
                try {
                    // Request _meta reaches the executor only on this authenticated
                    // loopback /mcp route (ADR-0246 U2c-7 human-approval marker).
                    const resultText = await executeTool(
                        tool.name,
                        args,
                        signal,
                        context.mcpReq._meta,
                    );
                    return {
                        content: [{ type: 'text', text: resultText }],
                    };
                } catch (error: unknown) {
                    if (signal.aborted) {
                        throw error;
                    }
                    const message = error instanceof Error
                        ? error.message
                        : String(error);
                    return {
                        content: [{ type: 'text', text: `Error: ${message}` }],
                        isError: true,
                    };
                }
            },
        );
    }

    return server;
}
