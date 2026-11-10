import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('@/hooks/useSystemMonitoring', () => ({
  useSystemMonitoring: () => ({
    latency: '1ms', connected: true, activeModels: [], healthStats: null, vaultHealth: null,
    tokenUsage: {},
    cacheHitRates: { Alice: { Anthropic: 0.5 } },
    pricing: { verified_at: '2026-11-10', has_override: false, estimated_models: [] },
    dailyQuota: null, backgroundActivity: {}, interruptedRuns: [], failedHandovers: [], memStats: null,
  }),
}));
import { SystemMonitoringView } from '../SystemMonitoringView';

describe('SystemMonitoringView（ADR-0311）', () => {
  it('Token 用量區放快取命中率與價目表資訊', () => {
    render(<SystemMonitoringView isActive />);
    expect(screen.getByTestId('cache-hit-Alice-Anthropic')).toHaveTextContent('50%');
    expect(screen.getByTestId('pricing-note')).toHaveTextContent('2026-11-10');
  });
});
