import { useState, useCallback } from 'react';
import { AgentEvent, LspStatus } from './useAgentSSE';

export function useLspHealth() {
  const [lspStatus, setLspStatus] = useState<LspStatus>({ pyright: 'idle', eslint: 'idle', ruff: 'idle' });

  const handleLspEvent = useCallback((event: AgentEvent) => {
    if (event.type === 'lsp_status') {
      setLspStatus(prev => ({ ...prev, [event.server]: event.status }));
    }
  }, []);

  return { lspStatus, setLspStatus, handleLspEvent };
}
