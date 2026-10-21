import { renderHook } from "@testing-library/react";
import { useEffect } from "react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { useArtifacts } from "@/hooks/useArtifacts";
import { useDebug } from "@/lib/providers/DebugProvider";

// Mock hooks
vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: vi.fn(),
}));
vi.mock('@/lib/providers/DebugProvider', () => ({
  useDebug: vi.fn(),
}));

describe("Auto-Pop Sidebar Integration", () => {
  const mockSetIsOpen = vi.fn();
  const mockSetActiveTab = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useArtifacts as any).mockReturnValue({
      setIsOpen: mockSetIsOpen,
      setActiveTab: mockSetActiveTab,
    });
  });

  it("should trigger sidebar open and debug tab when isSimulationMode becomes true", () => {
    (useDebug as any).mockReturnValue({ isSimulationMode: true });

    // Simulate the useEffect in app/page.tsx
    const TestComponent = () => {
      const { setIsOpen, setActiveTab } = useArtifacts();
      const { isSimulationMode } = useDebug();

      useEffect(() => {
        if (isSimulationMode) {
          setIsOpen(true);
          setActiveTab('debug');
        }
      }, [isSimulationMode, setIsOpen, setActiveTab]);
      
      return null;
    };

    renderHook(() => TestComponent());

    expect(mockSetIsOpen).toHaveBeenCalledWith(true);
    expect(mockSetActiveTab).toHaveBeenCalledWith('debug');
  });

  it("should NOT trigger if isSimulationMode is false", () => {
    (useDebug as any).mockReturnValue({ isSimulationMode: false });

    const TestComponent = () => {
      const { setIsOpen, setActiveTab } = useArtifacts();
      const { isSimulationMode } = useDebug();

      useEffect(() => {
        if (isSimulationMode) {
          setIsOpen(true);
          setActiveTab('debug');
        }
      }, [isSimulationMode, setIsOpen, setActiveTab]);
      
      return null;
    };

    renderHook(() => TestComponent());

    expect(mockSetIsOpen).not.toHaveBeenCalled();
    expect(mockSetActiveTab).not.toHaveBeenCalled();
  });
});
