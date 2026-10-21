import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import ChatPage from '@/app/page';

// 由於 page.tsx 會使用整個 App 的 providers 和 React-resizable-panels，
// 為了專注測試 Suspense fallback，我們可以特別 render page.tsx 並且 mock 掉其子組件。
// 當 ChatPage 渲染時，由於 React.lazy 或是 Suspense 機制，在 Mock 子組件載入期間，
// Suspense fallback 會被顯示。

vi.mock('@/lib/SystemStatusContext', () => ({
  useSystemStatus: () => {
    throw new Promise(() => {});
  }
}));

vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    activeSessionId: null,
    setAttachment: vi.fn()
  })
}));

vi.mock('@/lib/providers/ModelProvider', () => ({
  useModels: () => ({
    selectedModel: 'claude-3-5-sonnet'
  })
}));

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: () => ({
    isOpen: false,
    setIsOpen: vi.fn(),
    setActiveTab: vi.fn()
  })
}));

vi.mock('@/lib/providers/DebugProvider', () => ({
  useDebug: () => ({
    isSimulationMode: false
  })
}));

vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({
    sessions: [],
    setSessions: vi.fn(),
    activeSessionId: null,
    setActiveSessionId: vi.fn(),
    isLoading: false,
    refetchSessions: vi.fn()
  })
}));

vi.mock('@/lib/bridge', () => ({
  postToBridge: vi.fn(),
  isIDE: () => false
}));

describe('ChatPage Suspense Loading Fallback', () => {
  beforeEach(() => {
    // Mock window.matchMedia to prevent "TypeError: window.matchMedia is not a function"
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation(query => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(), // deprecated
        removeListener: vi.fn(), // deprecated
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
  });

  it('TDD-Red: renders suspense loading fallback with design-system compatible styles and spinning loader instead of plain text', () => {
    // 渲染 ChatPage，在 Suspense 載入期間會展示 fallback
    const { container } = render(<ChatPage />);
    
    // TDD 斷言：我們期望 Loading 不僅僅是 "Loading Touka..." 文字且只有 dark:text-white。
    // 我們期望它是一個帶有主題前景顏色（如 text-foreground 或 text-primary）的 premium 載入畫面。
    const loadingText = screen.queryByText('Loading Touka...');
    
    // 如果它有 text-foreground 或者是 layout-flex, 它就不應該只有純白 dark 樣式
    const loadingContainer = container.querySelector('.animate-pulse') || container.querySelector('svg') || container.firstChild;
    expect(loadingContainer).toBeInTheDocument();
    
    // 斷言不應只有 dark:text-white 的無對比純文字
    expect(loadingText).toBeNull();
    
    // 且應包含一個 animate-spin 的 Lucide 載入圖示
    const spinner = container.querySelector('.animate-spin');
    expect(spinner).toBeInTheDocument();
  });
});
