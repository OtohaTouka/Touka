import type { Request, Response, NextFunction } from 'express';
import { timingSafeEqual } from 'node:crypto';

/**
 * Factory that returns an Express middleware verifying Bearer token auth.
 *
 * An unavailable expected token fails closed. MCP endpoints never have an
 * unauthenticated fallback.
 */
export function createAuthMiddleware(expectedToken: string) {
    return (req: Request, res: Response, next: NextFunction): void => {
        if (!expectedToken) {
            res.status(503).json({ error: 'MCP authentication unavailable' });
            return;
        }

        const authHeader = req.headers.authorization as string | undefined;
        if (!authHeader?.startsWith('Bearer ')) {
            res.status(401).json({ error: 'Missing or invalid Authorization header' });
            return;
        }

        const token = authHeader.slice(7);
        const supplied = Buffer.from(token);
        const expected = Buffer.from(expectedToken);
        if (
            supplied.length !== expected.length
            || !timingSafeEqual(supplied, expected)
        ) {
            res.status(401).json({ error: 'Invalid token' });
            return;
        }

        next();
    };
}
