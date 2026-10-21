/**
 * G09（守衛涵蓋缺口待辦）— skill 審批事件必須在 dispatchSseLine 白名單、經 onEvent 分發。
 *
 * 缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G09）：
 *   ADR-0149：connections.ts 的 dispatchSseLine customEventTypes 白名單必須含
 *   "skill_creation_approval_request"（與 "skill_install_approval_request"）。被移除／
 *   改名即回到原始 bug：後端已發的部署審批事件被前端丟棄 → 無人解算 → request_approval
 *   180s auto-reject「你拒絕了」＝互動部署靜默失敗。此白名單與後端事件型別無程式同步機制。
 *
 *   此不變式**先前零測試**：guard_tool_events 只掃後端五型別、不做前後端 parity；唯一相關
 *   前端測試 EvolutionModal.creation 直接以 props 餵事件、不經 dispatchSseLine。
 *
 * dispatchSseLine 為 module-internal，故經 exported fetchChatStream 驅動真實分發路徑：
 * 餵 skill_creation/install 審批事件 → 斷言 onEvent 收到（白名單移除即 onEvent 不觸發、測試紅）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fetchChatStream } from '../connections';

const fetchMock = vi.fn();

function sseResponse(lines: string[]) {
  const body = lines.map(l => `data: ${l}\n`).join('') + 'data: [DONE]\n';
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

function run(): Promise<{ events: Record<string, unknown>[]; err?: unknown }> {
  return new Promise((resolve) => {
    const events: Record<string, unknown>[] = [];
    void fetchChatStream(
      [{ role: 'user', content: 'hi' }],
      { provider: 'proxy', baseUrl: 'http://proxy.test', model: 'm', protocol: 'openai' },
      () => {},                        // onChunk
      () => resolve({ events }),       // onDone
      (err) => resolve({ events, err }), // onError
      (e) => events.push(e),           // onEvent
    );
  });
}

describe('dispatchSseLine skill 審批事件白名單（ADR-0149／G09）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('skill_creation_approval_request 經 onEvent 分發（不被丟棄）', async () => {
    const evt = JSON.stringify({ type: 'skill_creation_approval_request', request_id: 'r-c' });
    fetchMock.mockResolvedValue(sseResponse([evt]));

    const { events, err } = await run();

    expect(err).toBeUndefined();
    const types = events.map(e => e.type);
    expect(types).toContain('skill_creation_approval_request');
  });

  it('skill_install_approval_request 同樣經 onEvent 分發（parity）', async () => {
    const evt = JSON.stringify({ type: 'skill_install_approval_request', request_id: 'r-i' });
    fetchMock.mockResolvedValue(sseResponse([evt]));

    const { events, err } = await run();

    expect(err).toBeUndefined();
    expect(events.map(e => e.type)).toContain('skill_install_approval_request');
  });
});
