import { renderHook, act } from "@testing-library/react";
import { useSystemMonitoring } from "@/hooks/useSystemMonitoring";
import * as connections from "@/lib/connections";
import { vi, describe, beforeEach, afterEach, it, expect } from "vitest";

// Mock connections lib
vi.mock("@/lib/connections", () => ({
  getActiveProxyUrl: vi.fn(() => "http://localhost:8000"),
  fetchTouka: vi.fn(),
}));

describe("useSystemMonitoring", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("should initialize with default stats", () => {
    const { result } = renderHook(() => useSystemMonitoring(false));
    expect(result.current.connected).toBe(false);
    expect(result.current.latency).toBe("...");
    expect(result.current.activeModels).toEqual([]);
  });

  it("should fetch data when activated", async () => {
    const mockFetch = connections.fetchTouka as any;
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ data: [], memories: [] }),
    });

    renderHook(() => useSystemMonitoring(true));

    expect(mockFetch).toHaveBeenCalled();
  });

  it("should set connected to false on fetch error", async () => {
    const mockFetch = connections.fetchTouka as any;
    mockFetch.mockRejectedValue(new Error("Network error"));

    const { result } = renderHook(() => useSystemMonitoring(true));

    await act(async () => {
      // Allow async effects to run
    });

    expect(result.current.connected).toBe(false);
    expect(result.current.latency).toBe("Error");
  });

  it("should set up polling interval when active", async () => {
    const mockFetch = connections.fetchTouka as any;
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({}),
    });

    renderHook(() => useSystemMonitoring(true));

    await act(async () => {
      vi.advanceTimersByTime(8000);
    });

    // Initial call + 1 interval call
    expect(mockFetch).toHaveBeenCalledTimes(2 * 7); // 7 endpoints
  });

  it("should clear interval on unmount or deactivation", () => {
    const clearIntervalSpy = vi.spyOn(global, "clearInterval");
    const { unmount } = renderHook(() => useSystemMonitoring(true));
    
    unmount();
    expect(clearIntervalSpy).toHaveBeenCalled();
  });
});







