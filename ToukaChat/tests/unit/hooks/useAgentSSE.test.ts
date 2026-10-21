import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAgentSSE, AgentEvent, ToolStep } from '@/hooks/useAgentSSE';

let activeStreamController: ReadableStreamDefaultController<Uint8Array> | null = null;
let activeAbortSignal: AbortSignal | null = null;

describe('useAgentSSE (Baseline)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    activeStreamController = null;
    activeAbortSignal = null;
    
    global.fetch = vi.fn((url: string, init?: RequestInit) => {
      if (url.startsWith('/stream')) {
        activeAbortSignal = init?.signal || null;
        const stream = new ReadableStream({
          start(controller) {
            activeStreamController = controller;
          }
        });
        return Promise.resolve({
          ok: true,
          body: stream,
        });
      }

      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ sessionId: 'test-session-1' }),
      });
    }) as unknown as typeof fetch;
  });

  const setupHook = () => {
    return renderHook(() => useAgentSSE({ postUrl: '/api', streamUrl: '/stream' }));
  };

  it('initializes with empty state', () => {
    const { result } = setupHook();
    expect(result.current.messages).toEqual([]);
    expect(result.current.terminalLines).toEqual([]);
    expect(result.current.lspStatus).toEqual({ pyright: 'idle', eslint: 'idle', ruff: 'idle' });
    expect(result.current.isConnected).toBe(false);
  });

  it('triggers sendMessage and connects to SSE', async () => {
    const { result } = setupHook();
    
    await act(async () => {
      await result.current.sendMessage('hello agent');
    });

    expect(global.fetch).toHaveBeenCalledWith('/api', expect.any(Object));
    expect(result.current.messages).toEqual([{ role: 'user', text: 'hello agent' }]);

    // 在 fetch /stream 成功建立後，isConnected 會變為 true
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    expect(result.current.isConnected).toBe(true);
  });

  it('processes agent_start and tool_start events', async () => {
    const { result } = setupHook();
    
    await act(async () => {
      await result.current.sendMessage('test');
    });

    await act(async () => {
      if (activeStreamController) {
        const text = `data: ${JSON.stringify({ type: 'agent_start', sessionId: '1' } as AgentEvent)}\n\n`;
        activeStreamController.enqueue(new TextEncoder().encode(text));
      }
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    expect(result.current.isAgentRunning).toBe(true);
    expect(result.current.messages[1]).toEqual({ role: 'agent', steps: [] });

    await act(async () => {
      if (activeStreamController) {
        const text = `data: ${JSON.stringify({ type: 'tool_start', tool: 'read_file', label: 'Reading file' } as AgentEvent)}\n\n`;
        activeStreamController.enqueue(new TextEncoder().encode(text));
      }
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    const agentMsg = result.current.messages[1] as { role: 'agent'; steps: ToolStep[] };
    expect(agentMsg.steps).toHaveLength(1);
    expect(agentMsg.steps[0]).toEqual({ tool: 'read_file', label: 'Reading file', status: 'running' });
  });

  it('processes file_diff event', async () => {
    const { result } = setupHook();
    
    await act(async () => {
      await result.current.sendMessage('test');
    });

    await act(async () => {
      if (activeStreamController) {
        const text = `data: ${JSON.stringify({ type: 'file_diff', path: 'test.ts', before: 'a', after: 'b' } as AgentEvent)}\n\n`;
        activeStreamController.enqueue(new TextEncoder().encode(text));
      }
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    expect(result.current.fileDiff).toEqual({ path: 'test.ts', before: 'a', after: 'b' });
  });

  it('processes terminal bash_output', async () => {
    const { result } = setupHook();
    
    await act(async () => {
      await result.current.sendMessage('test');
    });

    await act(async () => {
      if (activeStreamController) {
        const text = `data: ${JSON.stringify({ type: 'bash_output', line: 'npm run test', color: 'red' } as AgentEvent)}\n\n`;
        activeStreamController.enqueue(new TextEncoder().encode(text));
      }
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    expect(result.current.terminalLines).toHaveLength(1);
    expect(result.current.terminalLines[0]).toEqual({ text: 'npm run test', color: 'red' });
  });

  it('processes agent_done and closes connection', async () => {
    const { result } = setupHook();
    
    await act(async () => {
      await result.current.sendMessage('test');
    });

    await act(async () => {
      if (activeStreamController) {
        const text = `data: ${JSON.stringify({ type: 'agent_done', sessionId: '1' } as AgentEvent)}\n\n`;
        activeStreamController.enqueue(new TextEncoder().encode(text));
      }
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    expect(activeAbortSignal?.aborted).toBe(true);
  });
});
