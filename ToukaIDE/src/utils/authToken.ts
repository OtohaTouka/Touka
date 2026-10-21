import { randomBytes } from 'crypto';

/**
 * Generates a cryptographically secure random Bearer token (64 hex chars = 32 bytes).
 * Used as the shared secret between ToukaIDE's MCP server and ToukaProxy's IDE handler.
 */
export function generateAuthToken(): string {
    return randomBytes(32).toString('hex');
}
