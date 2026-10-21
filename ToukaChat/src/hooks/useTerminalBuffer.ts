import { useState, useCallback } from 'react';
import { AgentEvent, TerminalLine } from './useAgentSSE';

export function useTerminalBuffer() {
  const [terminalLines, setTerminalLines] = useState<TerminalLine[]>([]);

  const handleTerminalEvent = useCallback((event: AgentEvent) => {
    if (event.type === 'bash_output') {
      setTerminalLines(prev => [...prev, { text: event.line, color: event.color }]);
    }
  }, []);

  return { terminalLines, setTerminalLines, handleTerminalEvent };
}
