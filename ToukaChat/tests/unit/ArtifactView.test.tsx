import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import ArtifactView from '@/components/artifacts/ArtifactView';

// 使用 vi.hoisted 確保變數在 mock 載入前已正確初始化
const { mockArtifactsState } = vi.hoisted(() => {
  return {
    mockArtifactsState: {
      artifacts: [
        {
          id: 'artifact_web_123',
          title: 'Test Web Artifact',
          type: 'web',
          content: '<div>Interactive App</div>',
        }
      ],
      currentId: 'artifact_web_123'
    }
  };
});

// Mock useArtifacts
vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: () => ({
    artifacts: mockArtifactsState.artifacts,
    currentArtifactId: mockArtifactsState.currentId,
    setCurrentArtifact: vi.fn(),
    // 預設 activeTab
    activeTab: 'artifacts'
  })
}));

// Mock connections.ts
vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(() => {
    // 預設失敗以測試錯誤條
    return Promise.reject(new Error('Mock network error'));
  }),
}));

// Mock bridge.ts
vi.mock('@/lib/bridge', () => ({
  bridgeApplyArtifact: vi.fn(() => Promise.resolve(false)), // 預設失敗以測試錯誤條
  isIDE: vi.fn(() => false),
}));

// 流程圖在沙箱框裡畫（ADR-0294）
vi.mock('@/lib/artifacts/mermaidSandbox', () => ({
  renderMermaidInSandbox: vi.fn(),
}));

describe('ArtifactView (Frontend Sandbox Defense)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 次序污染修復：mockArtifactsState 為共享可變物件，DA10/Markdown 測試會改寫它；
    // 隨機次序下（vitest sequence.shuffle）若 Markdown 先跑，iframe 測試就找不到
    // Web Artifact。每測試前重置為預設 web artifact，使所有測試自足、與次序無關。
    mockArtifactsState.artifacts = [
      {
        id: 'artifact_web_123',
        title: 'Test Web Artifact',
        type: 'web',
        content: '<div>Interactive App</div>',
      },
    ];
    mockArtifactsState.currentId = 'artifact_web_123';
  });

  it('renders web iframe with restricted sandbox attributes to prevent same-origin escape', () => {
    render(<ArtifactView />);
    
    // 尋找渲染出來的 iframe
    const iframe = screen.getByTitle('Web Artifact') as HTMLIFrameElement;
    expect(iframe).toBeInTheDocument();
    
    // 取得 sandbox 屬性
    const sandboxAttr = iframe.getAttribute('sandbox');
    
    // TDD 斷言：
    // 1. 必須存在 sandbox 屬性以實現防禦。
    expect(sandboxAttr).not.toBeNull();
    
    // 2. 為了安全性，絕對不可包含 allow-same-origin，否則將導致同源逃逸 (XSS / Hijack) 漏洞！
    if (sandboxAttr) {
      const tokens = sandboxAttr.split(/\s+/);
      expect(tokens).not.toContain('allow-same-origin');
      
      // 3. 同時應具備 allow-scripts 以允許正常的 JS 腳本執行 (以渲染 UI 應用)
      expect(tokens).toContain('allow-scripts');
    }
  });

  // DA10 (audit #15): 無 allow-same-origin 已擋下 token 竊取，殘留風險＝釣魚/下載/beacon。
  // 收緊 sandbox 並以 CSP 封死網路外洩與表單行為。
  it('DA10: web iframe sandbox must drop allow-forms and allow-downloads (phishing/drive-by surface)', () => {
    mockArtifactsState.artifacts = [
      { id: 'artifact_web_da10', title: 'Web', type: 'web', content: '<div>App</div>' },
    ];
    mockArtifactsState.currentId = 'artifact_web_da10';

    render(<ArtifactView />);
    const iframe = screen.getByTitle('Web Artifact') as HTMLIFrameElement;
    const tokens = (iframe.getAttribute('sandbox') ?? '').split(/\s+/);

    expect(tokens).toContain('allow-scripts');
    expect(tokens).not.toContain('allow-same-origin');
    expect(tokens).not.toContain('allow-forms');
    expect(tokens).not.toContain('allow-downloads');
  });

  it('DA10: web iframe srcdoc must inject a CSP that blocks network egress and form actions', async () => {
    mockArtifactsState.artifacts = [
      { id: 'artifact_web_da10b', title: 'Web', type: 'web', content: '<div>App</div>' },
    ];
    mockArtifactsState.currentId = 'artifact_web_da10b';

    render(<ArtifactView />);
    const iframe = screen.getByTitle('Web Artifact') as HTMLIFrameElement;

    // srcdoc 由 useEffect 命令式設定，等待寫入完成
    await waitFor(() => expect(iframe.getAttribute('srcdoc') ?? '').toContain('Content-Security-Policy'));

    const doc = iframe.getAttribute('srcdoc') ?? '';
    expect(doc).toMatch(/http-equiv=["']Content-Security-Policy["']/);
    // 封死 fetch/XHR/WebSocket/sendBeacon 外洩通道
    expect(doc).toContain("connect-src 'none'");
    // 封死表單釣魚/外洩（與移除 allow-forms 互為縱深）
    expect(doc).toContain("form-action 'none'");
    // 預設拒絕，僅放行渲染必需資源
    expect(doc).toContain("default-src 'none'");
  });

  it('TDD-Red: renders header with theme-compatible background color and no bg-white', () => {
    const { container } = render(<ArtifactView />);
    
    // 取得外層容器
    const rootDiv = container.firstChild as HTMLDivElement;
    expect(rootDiv).toBeInTheDocument();
    
    // 斷言：不應該寫死 bg-white，應該使用 bg-card 或主題變數
    expect(rootDiv.className).not.toContain('bg-white');
  });

  it('TDD-Red: renders close button with Lucide X icon when error status is active', async () => {
    render(<ArtifactView />);
    
    // 點擊 "Apply to Project" 以觸發錯誤（因為 mock 設為失敗）
    const applyButton = screen.getByRole('button', { name: /Apply to Project/i });
    fireEvent.click(applyButton);
    
    // 驗證錯誤訊息出現（errorMsg("SYNC_FAILED")，測試環境固定 zh-TW → 在地化中文）
    await screen.findByText('資料同步失敗');

    
    // TDD 斷言：錯誤條的關閉按鈕應該使用 X 圖示，而不是 ChevronRight
    // X 圖示有 lucide-x class 名，且不應該有 ChevronRight 圖示
    const closeSvg = screen.getByRole('button', { name: '' }).querySelector('svg');
    expect(closeSvg).toBeInTheDocument();
    expect(closeSvg?.classList.contains('lucide-x')).toBe(true);
  });

  it('TDD-Red: MarkdownRenderer renders structured HTML elements instead of raw text in whitespace-pre-wrap', () => {
    mockArtifactsState.artifacts = [
      {
        id: 'artifact_md_123',
        title: 'Test Markdown Artifact',
        type: 'markdown',
        content: '# Heading 1\n* Item 1\n* Item 2',
      }
    ];
    mockArtifactsState.currentId = 'artifact_md_123';

    const { container } = render(<ArtifactView />);
    
    // 驗證是否有渲染真正的 h1 標籤
    const h1 = container.querySelector('h1');
    expect(h1).toBeInTheDocument();
    expect(h1?.textContent).toBe('Heading 1');
    
    // 驗證是否有渲染真正的 li 標籤
    const liList = container.querySelectorAll('li');
    expect(liList.length).toBe(2);
  });
});


