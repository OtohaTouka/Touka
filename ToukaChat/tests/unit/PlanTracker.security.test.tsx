import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, act } from '@testing-library/react';
import React from 'react';
import { PlanTracker } from '@/components/PlanTracker';

describe('P10 — PlanTracker postMessage origin check', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('rejects postMessage from cross-origin', () => {
    const { rerender } = render(<PlanTracker />);

    // Dispatch a message event from a cross-origin
    act(() => {
      const event = new MessageEvent('message', {
        data: {
          type: 'plan_created',
          title: 'Evil plan',
          steps: ['do bad things'],
        },
        origin: 'https://evil.example.com',
      });
      window.dispatchEvent(event);
    });

    // Plan should NOT be rendered because origin was rejected
    rerender(<PlanTracker />);
    const planTitle = document.body.textContent;
    expect(planTitle).not.toContain('Evil plan');
  });

  it('accepts postMessage from same origin', () => {
    render(<PlanTracker />);

    act(() => {
      const event = new MessageEvent('message', {
        data: {
          type: 'plan_created',
          title: 'Legit plan',
          steps: ['do good things'],
        },
        origin: window.location.origin,
      });
      window.dispatchEvent(event);
    });

    expect(document.body.textContent).toContain('Legit plan');
  });
});
