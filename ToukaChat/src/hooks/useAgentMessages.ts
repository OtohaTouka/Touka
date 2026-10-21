import { useState, useCallback } from 'react';
import { AgentEvent, ChatMessage, FileDiff } from './useAgentSSE';
import { SkillInstallRequest } from '../lib/types';

export function useAgentMessages() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isAgentRunning, setIsAgentRunning] = useState(false);
  const [fileDiff, setFileDiff] = useState<FileDiff | null>(null);
  const [pendingInstall, setPendingInstall] = useState<SkillInstallRequest | null>(null);

  const clearFileDiff = useCallback(() => setFileDiff(null), []);

  const handleMessageEvent = useCallback((event: AgentEvent) => {
    switch (event.type) {
      case 'agent_start':
        setIsAgentRunning(true);
        setMessages(prev => [...prev, { role: 'agent', steps: [] }]);
        break;

      case 'agent_done':
        setIsAgentRunning(false);
        break;

      case 'tool_start':
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === 'agent') {
            updated[updated.length - 1] = {
              ...last,
              steps: [...last.steps, { tool: event.tool, label: event.label, status: 'running' }],
            };
          }
          return updated;
        });
        break;

      case 'tool_done':
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === 'agent') {
            updated[updated.length - 1] = {
              ...last,
              steps: last.steps.map(s =>
                s.label === event.label && s.status === 'running'
                  ? { ...s, status: 'done', output: event.output }
                  : s
              ),
            };
          }
          return updated;
        });
        break;

      case 'tool_error':
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === 'agent') {
            updated[updated.length - 1] = {
              ...last,
              steps: last.steps.map(s =>
                s.label === event.label && s.status === 'running'
                  ? { ...s, status: 'error', output: event.error }
                  : s
              ),
            };
          }
          return updated;
        });
        break;

      case 'file_diff':
        setFileDiff({ path: event.path, before: event.before, after: event.after });
        break;

      case 'ai_message':
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === 'agent') {
            updated[updated.length - 1] = {
              role: 'ai',
              text: event.text,
              model: event.model,
              steps: last.steps,
            };
          } else {
            updated.push({ role: 'ai', text: event.text, model: event.model, steps: [] });
          }
          return updated;
        });
        break;

      case 'skill_install_approval_request':
        setPendingInstall(event);
        break;
    }
  }, []);

  return { 
    messages, setMessages, 
    isAgentRunning, setIsAgentRunning, 
    fileDiff, setFileDiff, clearFileDiff,
    pendingInstall, setPendingInstall,
    handleMessageEvent 
  };
}
