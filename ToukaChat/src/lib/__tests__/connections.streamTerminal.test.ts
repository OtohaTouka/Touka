import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { captureRequestScope, clearAccessToken, fetchChatStream, resumeChatStream, setAccessToken } from '../connections';
import type { ChatOptions } from '../connections';
import type { StreamStartedEvent, StreamTerminalEvent } from '../types';

const stored = vi.hoisted(() => new Map<string, unknown>());
vi.mock('@/hooks/useLocalStorage', () => ({
  getLocalStorageItem: (key: string, fallback: unknown) => stored.get(key) ?? fallback,
  setLocalStorageItem: (key: string, value: unknown) => stored.set(key, value),
}));
vi.mock('../error_handler', () => ({ logSSOTError: vi.fn() }));
vi.mock('../time_utils', () => ({ getSystemEpoch: () => 1700000000000 }));
const wire = vi.fn();
const token = (sub: string) => `e30.${btoa(JSON.stringify({ sub, type: 'access', exp: 1700003600 }))}.synthetic`;
const started: StreamStartedEvent = { type: 'touka_stream_started', session_id: 's', stream_id: 'g', user_parent_id: 'u', assistant_node_id: 'a' };
const terminal: StreamTerminalEvent = { ...started, type: 'touka_stream_terminal', generation: 'completed',
  persistence: { status: 'saved', representation: 'text', revision: 'r2' } };
const frame = (value: unknown) => `data: ${JSON.stringify(value)}\n\n`;
function response(text: string, fragmented = false) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream<Uint8Array>({ start(controller) {
    if (fragmented) for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
    else controller.enqueue(bytes);
    controller.close();
  } }));
}
function options(): ChatOptions {
  return { provider: 'proxy', baseUrl: 'http://x', model: 'test', session_id: 's',
    ui_persistence: { mode: 'persistent', revision: 'r1', user_parent_id: 'u', assistant_node_id: 'a' },
    requestScope: captureRequestScope() };
}
async function consume(text: string, resume = false, fragmented = false) {
  wire.mockResolvedValue(response(text, fragmented));
  const chunk = vi.fn(), done = vi.fn(), error = vi.fn(), event = vi.fn();
  if (resume) await resumeChatStream('http://x', 's', -1, chunk, done, error, event, undefined, undefined,
    { started, requestScope: captureRequestScope() });
  else await fetchChatStream([], options(), chunk, done, error, event);
  return { chunk, done, error, event };
}

describe('typed UI terminal through real fetch/decoder', () => {
  beforeEach(() => {
    vi.clearAllMocks(); stored.clear();
    stored.set('touka_connections', [{ id: 'p', provider: 'proxy', enabled: true, url: 'http://x' }]);
    vi.stubGlobal('fetch', wire); setAccessToken(token('A'));
  });
  afterEach(() => { clearAccessToken(); vi.unstubAllGlobals(); });
  it.each([false, true])('flushes UTF8 fragments and final terminal without newline; resume=%s', async resume => {
    const result = await consume(frame(started) + frame({ choices: [{ delta: { content: '繁體字' } }] }) + frame(terminal).trimEnd(), resume, true);
    expect(result.chunk.mock.calls.flat().join('')).toBe('繁體字');
    expect(result.done).toHaveBeenCalledExactlyOnceWith(terminal);
    expect(result.event).toHaveBeenCalledWith(started);
    expect(result.error).not.toHaveBeenCalled();
    if (resume) expect(wire.mock.calls[0][0]).toContain('stream_id=g');
    else {
      const body = JSON.parse(wire.mock.calls[0][1].body);
      expect(body.ui_persistence).toEqual(options().ui_persistence);
      expect(body).not.toHaveProperty('requestScope');
    }
  });
  it.each(['', 'data: [DONE]\n\n', frame({ ...terminal, stream_id: 'foreign' }), frame({ ...terminal, persistence: { status: 'saved' } })])(
    'does not turn missing/foreign/malformed terminal into success %s', async ending => {
      const result = await consume(frame(started) + ending);
      expect(result.done).toHaveBeenCalledTimes(1);
      expect(result.done.mock.calls[0][0]).toBeUndefined();
    });
  it('ignores foreign start and duplicate terminal without admitting another generation', async () => {
    const result = await consume(frame({ ...started, assistant_node_id: 'other' }) + frame(started) + frame(terminal) + frame(terminal));
    expect(result.done).toHaveBeenCalledExactlyOnceWith(terminal);
    expect(result.event.mock.calls.filter(([event]) => event.type === 'touka_stream_started')).toEqual([[started]]);
  });
  it('does not send captured A prompt with B credential after debug await', async () => {
    const error = vi.fn();
    await fetchChatStream([], options(), vi.fn(), vi.fn(), error, undefined, undefined, async () => {
      setAccessToken(token('B'));
    });
    expect(wire).not.toHaveBeenCalled();
    expect(error).toHaveBeenCalledWith(expect.objectContaining({ name: 'AbortError' }));
  });
  it('resumes after a cursor without replaying started', async () => {
    wire.mockResolvedValue(response(frame({ choices: [{ delta: { content: 'continued' } }] }) + frame(terminal)));
    const chunk = vi.fn(), done = vi.fn();
    await resumeChatStream('http://x', 's', 3, chunk, done, vi.fn(), vi.fn(), undefined, undefined,
      { started, requestScope: captureRequestScope() });
    expect(chunk).toHaveBeenCalledExactlyOnceWith('continued');
    expect(done).toHaveBeenCalledExactlyOnceWith(terminal);
  });
  it('keeps an explicitly captured null scope rejected even after another login', async () => {
    const error = vi.fn();
    const captured = { ...options(), requestScope: null };
    setAccessToken(token('B'));
    await fetchChatStream([], captured, vi.fn(), vi.fn(), error);
    expect(wire).not.toHaveBeenCalled();
    expect(error).toHaveBeenCalledWith(expect.objectContaining({ name: 'AbortError' }));
  });
});
