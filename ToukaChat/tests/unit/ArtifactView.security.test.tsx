import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MERMAID_CONFIG } from '@/lib/artifacts/mermaidConfig';
import { renderMermaidInSandbox } from '@/lib/artifacts/mermaidSandbox';

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

// 流程圖在沙箱框裡畫（ADR-0294）；這裡直接給框回傳的 SVG 字串
vi.mock('@/lib/artifacts/mermaidSandbox', () => ({
  renderMermaidInSandbox: vi.fn(),
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
    // 沙箱框裡套用的設定（mermaidSandbox.ts 把 MERMAID_CONFIG 送進框）
    expect(MERMAID_CONFIG.securityLevel).toBe('strict');
  });

  it('renders the diagram in the sandbox frame and sanitizes the returned SVG', async () => {
    vi.mocked(renderMermaidInSandbox).mockResolvedValue(
      '<svg id="test"><text>A</text><image href="https://evil.example/x.png"/></svg>',
    );
    const { container } = render(<ArtifactView />);
    await waitFor(() => expect(container.querySelector('svg#test')).not.toBeNull());
    expect(renderMermaidInSandbox).toHaveBeenCalledWith(expect.any(String), 'graph TD; A-->B');
    expect(container.querySelector('svg#test')?.textContent).toBe('A');
    expect(container.innerHTML).not.toContain('evil.example');
  });

  it('MermaidRenderer sanitizes the SVG through sanitizeMermaidSvg (DOMPurify)', async () => {
    // Static verification: the renderer hands the SVG to the shared sanitizer (ADR-0294),
    // and the sanitizer is built on DOMPurify.
    const { readFileSync } = await import('fs');
    const { resolve } = await import('path');
    const src = readFileSync(
      resolve(process.cwd(), 'src/components/artifacts/ArtifactView.tsx'),
      'utf-8',
    );
    expect(src).toContain("import { sanitizeMermaidSvg } from '@/lib/artifacts/sanitizeMermaidSvg'");
    expect(src).toContain('sanitizeMermaidSvg(svg)');
    expect(src).toContain('renderMermaidInSandbox(');
    const sanitizer = readFileSync(
      resolve(process.cwd(), 'src/lib/artifacts/sanitizeMermaidSvg.ts'),
      'utf-8',
    );
    expect(sanitizer).toContain('from "dompurify"');
    expect(sanitizer).toContain('.sanitize(svg, PURIFY_CONFIG)');
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
