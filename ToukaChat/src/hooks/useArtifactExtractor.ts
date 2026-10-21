import { useRef, useEffect, useCallback } from 'react';
import { useArtifacts } from './useArtifacts';
import { extractArtifacts, ExtractedArtifact } from '../lib/parsers/artifact-extractor';
import { TreeHistory, getLinearMessages } from '../lib/tree_history';
import { logSSOTError } from '@/lib/error_handler';
import { captureRequestScope, isRequestScopeCurrent, subscribeCredentialScope } from '@/lib/connections';

export function useArtifactExtractor(activeSessionId: string, isSending: boolean, history: TreeHistory) {
  const activeStreamingArtifacts = useRef<Map<number, string>>(new Map());
  const scope = useRef(captureRequestScope());
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const invalidate = () => {
      if (!isRequestScopeCurrent(scope.current)) {
        activeStreamingArtifacts.current.clear();
        useArtifacts.getState().clearArtifacts(true);
      }
    };
    const unsubscribe = subscribeCredentialScope(invalidate);
    window.addEventListener('touka_connections_updated', invalidate);
    window.addEventListener('storage', invalidate);
    invalidate();
    return () => {
      mounted.current = false;
      unsubscribe();
      window.removeEventListener('touka_connections_updated', invalidate);
      window.removeEventListener('storage', invalidate);
    };
  }, []);

  const processStreamChunk = useCallback((aiText: string) => {
    if (!mounted.current || !isRequestScopeCurrent(scope.current)) return;
    try {
      const { addArtifact, updateArtifact, setCurrentArtifact } = useArtifacts.getState();
      if (aiText.length > 0 && (aiText.includes("```") || aiText.includes("<touka_artifact"))) {
        const discovered = extractArtifacts(aiText);
        let lastAid = null;
        discovered.forEach((art, idx) => {
          if (!activeStreamingArtifacts.current.has(idx)) {
            const aid = addArtifact(art);
            activeStreamingArtifacts.current.set(idx, aid);
            lastAid = aid;
          } else { 
            const aid = activeStreamingArtifacts.current.get(idx)!;
            updateArtifact(aid, art.content); 
            lastAid = aid;
          }
        });
        if (lastAid) {
            setCurrentArtifact(lastAid);
        }
      }
    } catch (error) {
      logSSOTError(error, "useArtifactExtractor:processStreamChunk");
    }
  }, []);

  const finalizeStream = useCallback((aiText: string) => {
    if (!mounted.current || !isRequestScopeCurrent(scope.current)) return;
    const discovered = extractArtifacts(aiText);
    const { addArtifact, updateArtifact, setCurrentArtifact } = useArtifacts.getState();
    let lastAid = null;
    discovered.forEach((art, idx) => {
      if (!activeStreamingArtifacts.current.has(idx)) { 
        const aid = addArtifact(art); 
        activeStreamingArtifacts.current.set(idx, aid); 
        lastAid = aid;
      } else { 
        const aid = activeStreamingArtifacts.current.get(idx)!; 
        updateArtifact(aid, art.content); 
        lastAid = aid; 
      }
    });
    if (lastAid) setCurrentArtifact(lastAid);
    window.dispatchEvent(new CustomEvent("touka_open_artifacts"));
    activeStreamingArtifacts.current.clear();
  }, []);

  useEffect(() => {
    const { clearArtifacts, loadArtifacts } = useArtifacts.getState();
    if (!isRequestScopeCurrent(scope.current)) return;
    if (!activeSessionId) {
      activeStreamingArtifacts.current.clear();
      clearArtifacts();
      return;
    }
    if (isSending) return;
    clearArtifacts();
    const linear = getLinearMessages(history);
    const allExtracted: ExtractedArtifact[] = [];
    linear.forEach(m => {
      if (m.role === 'assistant' && m.content) {
        const found = extractArtifacts(m.content);
        found.forEach(art => { 
            if (!allExtracted.find(ea => ea.content === art.content)) allExtracted.push(art); 
        });
      }
    });
    loadArtifacts(allExtracted);
  }, [activeSessionId, isSending, history]);

  return { processStreamChunk, finalizeStream };
}
