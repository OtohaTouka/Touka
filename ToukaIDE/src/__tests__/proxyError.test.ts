import { describe, expect, it } from 'vitest';
import { proxyErrorMessage } from '../utils/proxyError';

const reply = (status: number, body: string) => new Response(body, { status });

describe('proxyErrorMessage', () => {
  it('shows what the backend said', async () => {
    const body = JSON.stringify({ detail: { code: 'SESSION_OLD_HOME_READ_ONLY', message: 'read only' } });
    expect(await proxyErrorMessage(reply(409, body))).toBe('read only');
  });

  it('accepts a plain text detail', async () => {
    expect(await proxyErrorMessage(reply(400, JSON.stringify({ detail: 'bad request' })))).toBe('bad request');
  });

  it.each(['not json', '{}', JSON.stringify({ detail: { code: 'X' } })])('falls back to the status for %s', async (body) => {
    expect(await proxyErrorMessage(reply(502, body))).toBe('Proxy Error: 502');
  });
});
