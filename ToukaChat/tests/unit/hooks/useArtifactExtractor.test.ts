import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useArtifactExtractor } from '@/hooks/useArtifactExtractor';
import { useArtifacts } from '@/hooks/useArtifacts';

vi.mock('@/lib/connections', () => ({
  captureRequestScope: () => ({ proxyOrigin: 'http://synthetic', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true,
  subscribeCredentialScope: () => () => {},
}));

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: {
    getState: vi.fn(),
  }
}));

vi.mock('@/lib/parsers/artifact-extractor', () => ({
  extractArtifacts: vi.fn((text: string) => {
    if (text.includes('```')) {
      return [{ content: text, type: 'code' }];
    }
    return [];
  }),
}));

describe('useArtifactExtractor', () => {
  let mockClearArtifacts: ReturnType<typeof vi.fn>;
  let mockLoadArtifacts: ReturnType<typeof vi.fn>;
  let mockAddArtifact: ReturnType<typeof vi.fn>;
  let mockUpdateArtifact: ReturnType<typeof vi.fn>;
  let mockSetCurrentArtifact: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockClearArtifacts = vi.fn();
    mockLoadArtifacts = vi.fn();
    mockAddArtifact = vi.fn().mockReturnValue('art-1');
    mockUpdateArtifact = vi.fn();
    mockSetCurrentArtifact = vi.fn();

    (useArtifacts.getState as any).mockReturnValue({
      clearArtifacts: mockClearArtifacts,
      loadArtifacts: mockLoadArtifacts,
      addArtifact: mockAddArtifact,
      updateArtifact: mockUpdateArtifact,
      setCurrentArtifact: mockSetCurrentArtifact,
    });
  });

  it('scans history and loads artifacts on change', () => {
    const history = {
      messages: {
        '1': { id: '1', role: 'assistant', content: '```\ncode\n```', childrenIds: [] },
      },
      currentId: '1'
    };

    renderHook(() => useArtifactExtractor('session-1', false, history as any));

    expect(mockClearArtifacts).toHaveBeenCalled();
    expect(mockLoadArtifacts).toHaveBeenCalledWith([{ content: '```\ncode\n```', type: 'code' }]);
  });

  it('processes stream chunk and adds artifact', () => {
    const { result } = renderHook(() => useArtifactExtractor('session-1', true, { messages: {}, currentId: null }));

    result.current.processStreamChunk('```\ncode chunk 1');
    expect(mockAddArtifact).toHaveBeenCalledWith({ content: '```\ncode chunk 1', type: 'code' });
    expect(mockSetCurrentArtifact).toHaveBeenCalledWith('art-1');

    result.current.processStreamChunk('```\ncode chunk 2');
    expect(mockUpdateArtifact).toHaveBeenCalledWith('art-1', '```\ncode chunk 2');
  });

  it('finalizes stream properly', () => {
    const { result } = renderHook(() => useArtifactExtractor('session-1', true, { messages: {}, currentId: null }));
    const dispatchSpy = vi.spyOn(window, 'dispatchEvent');

    result.current.finalizeStream('```\nfinal code');
    expect(mockAddArtifact).toHaveBeenCalledWith({ content: '```\nfinal code', type: 'code' });
    expect(dispatchSpy).toHaveBeenCalled();
  });
});







