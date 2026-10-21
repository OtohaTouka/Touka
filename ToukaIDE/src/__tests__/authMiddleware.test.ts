import { describe, it, expect, vi } from 'vitest';
import { createAuthMiddleware } from '../utils/authMiddleware';

const TOKEN = 'abc123testtoken';

function makeReq(authHeader?: string): Record<string, unknown> {
    return { headers: { authorization: authHeader } };
}

function makeRes() {
    const res = {
        statusCode: 200,
        body: null as unknown,
        status(code: number) { this.statusCode = code; return this; },
        json(body: unknown) { this.body = body; return this; },
        send(body: unknown) { this.body = body; return this; },
    };
    return res;
}

describe('createAuthMiddleware', () => {
    it('calls next() when valid Bearer token is provided', () => {
        const middleware = createAuthMiddleware(TOKEN);
        const req = makeReq(`Bearer ${TOKEN}`);
        const res = makeRes();
        const next = vi.fn();

        middleware(req as never, res as never, next);

        expect(next).toHaveBeenCalledOnce();
        expect(res.statusCode).toBe(200);
    });

    it('returns 401 when Authorization header is missing', () => {
        const middleware = createAuthMiddleware(TOKEN);
        const req = makeReq(undefined);
        const res = makeRes();
        const next = vi.fn();

        middleware(req as never, res as never, next);

        expect(next).not.toHaveBeenCalled();
        expect(res.statusCode).toBe(401);
    });

    it('returns 401 when token is wrong', () => {
        const middleware = createAuthMiddleware(TOKEN);
        const req = makeReq('Bearer wrong-token');
        const res = makeRes();
        const next = vi.fn();

        middleware(req as never, res as never, next);

        expect(next).not.toHaveBeenCalled();
        expect(res.statusCode).toBe(401);
    });

    it('returns 401 when Authorization format is not Bearer', () => {
        const middleware = createAuthMiddleware(TOKEN);
        const req = makeReq(`Basic ${TOKEN}`);
        const res = makeRes();
        const next = vi.fn();

        middleware(req as never, res as never, next);

        expect(next).not.toHaveBeenCalled();
        expect(res.statusCode).toBe(401);
    });

    it('fails closed when the expected token is unavailable', () => {
        const middleware = createAuthMiddleware('');
        const req = makeReq(undefined);
        const res = makeRes();
        const next = vi.fn();

        middleware(req as never, res as never, next);

        expect(next).not.toHaveBeenCalled();
        expect(res.statusCode).toBe(503);
        expect(res.body).toEqual({ error: 'MCP authentication unavailable' });
    });
});
