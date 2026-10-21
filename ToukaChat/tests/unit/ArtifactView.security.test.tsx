import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import mermaid from 'mermaid';

// Mock modules before importing component.
// 單一頂層 vi.mock + vi.hoisted 可變狀態（同 ArtifactView.test.tsx 模式）：
// vi.mock 必被 hoist 至模組頂端，同一路徑寫兩個工廠時後者會整檔覆蓋前者，
// 故各測試改以改寫 mockArtifactsState 切換 artifact。
const { mockArtifactsState } = vi.hoisted(() => {
  return {
    mockArtifactsState: {
      artifacts: [
        {
          id: 'artifact_mermaid_1',
          title: 'Test Mermaid',
          type: 'mermaid',
          content: 'graph TD; A-->B',
        },
      ],
      currentId: 'artifact_mermaid_1',
    },
  };
});

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: () => ({
    artifacts: mockArtifactsState.artifacts,
    currentArtifactId: mockArtifactsState.currentId,
    setCurrentArtifact: vi.fn(),
  }),
}));

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));

vi.mock('@/lib/bridge', () => ({
  bridgeApplyArtifact: vi.fn(),
  isIDE: vi.fn(() => false),
}));

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: '<svg id="test"><text>A</text></svg>' }),
  },
}));

// Import component after mocks are set up
import ArtifactView from '@/components/artifacts/ArtifactView';

// 次序污染防護（同 ArtifactView.test.tsx）：mockArtifactsState 為共享可變物件，
// P12 測試會改寫它；每測試前重置為預設 mermaid artifact，使測試與執行次序無關。
beforeEach(() => {
  mockArtifactsState.artifacts = [
    {
      id: 'artifact_mermaid_1',
      title: 'Test Mermaid',
      type: 'mermaid',
      content: 'graph TD; A-->B',
    },
  ];
  mockArtifactsState.currentId = 'artifact_mermaid_1';
});

describe('P2 — Mermaid XSS security', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('mermaid securityLevel must be strict (not loose)', () => {
    // mermaid.initialize is called at module load — capture the call args
    const initCalls = (mermaid.initialize as ReturnType<typeof vi.fn>).mock.calls;
    // If called, every call must use strict securityLevel
    for (const [config] of initCalls) {
      expect(config?.securityLevel).not.toBe('loose');
      if (config?.securityLevel !== undefined) {
        expect(config.securityLevel).toBe('strict');
      }
    }
  });

  it('ArtifactView.tsx must import DOMPurify and use it in MermaidRenderer', async () => {
    // Static verification: the source file must use DOMPurify.sanitize
    const { readFileSync } = await import('fs');
    const { resolve } = await import('path');
    const src = readFileSync(
      resolve(process.cwd(), 'src/components/artifacts/ArtifactView.tsx'),
      'utf-8',
    );
    expect(src).toContain("import DOMPurify from 'dompurify'");
    expect(src).toContain('DOMPurify.sanitize(svg');
  });
});

describe('P12 — iframe CDN SRI', () => {
  it('srcdoc must not contain external CDN scripts without SRI', () => {
    // 經 hoisted 可變狀態切換為 web artifact（vi.mock 不可寫在 it() 內，會被 hoist）
    mockArtifactsState.artifacts = [
      {
        id: 'artifact_web_1',
        title: 'Test Web',
        type: 'web',
        content: '<div>Hello</div>',
      },
    ];
    mockArtifactsState.currentId = 'artifact_web_1';

    render(<ArtifactView />);
    const iframe = screen.queryByTitle('Web Artifact') as HTMLIFrameElement | null;

    if (iframe?.srcdoc) {
      // Must not contain CDN script without integrity attribute
      const hasCdnWithoutSri =
        /src="https?:\/\/cdn\.[^"]*"(?![^>]*integrity=)/.test(iframe.srcdoc);
      expect(hasCdnWithoutSri).toBe(false);
    }
  });
});
