import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { AutonomyBudget } from '../AutonomyBudget';
import type { AutonomyConfig } from '@/lib/types';

// i18n：回傳 key 以便穩定斷言（ADR-0094 成本上限卡）
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

// Radix Slider 在 jsdom 需要 ResizeObserver；替換為原生 range（比照 SecurityTab.test）。
vi.mock('@/components/ui/slider', async () => {
  const { createElement } = await import('react');
  const Slider = ({ onValueChange, value, min = 0, max = 100, step = 1 }: {
    onValueChange?: (v: number[]) => void;
    value?: number[];
    min?: number;
    max?: number;
    step?: number;
  }) =>
    createElement('input', {
      type: 'range',
      min,
      max,
      step,
      value: value?.[0] ?? 0,
      onChange: (e: React.ChangeEvent<HTMLInputElement>) => onValueChange?.([Number(e.target.value)]),
    });
  return { Slider };
});

function makeConfig(cap?: { enabled: boolean; max_total_cost_usd: number | null }): AutonomyConfig {
  return {
    // ADR-0146：工具輪保險絲（0＝不限）；nudge 舊鍵已隨機制移除
    react_loop: { max_tool_rounds: 0 },
    self_repair: { enabled: true, max_attempts: 3, repair_prompt: '' },
    ...(cap ? { cap } : {}),
  } as unknown as AutonomyConfig;
}

describe('AutonomyBudget cost cap (ADR-0094)', () => {
  it('cap 關閉時不顯示 USD 輸入；切換開關會以 enabled=true 回呼', () => {
    const setAutonomyConfig = vi.fn();
    render(
      <AutonomyBudget
        autonomyConfig={makeConfig({ enabled: false, max_total_cost_usd: null })}
        setAutonomyConfig={setAutonomyConfig}
        isSavingAutonomy={false}
        saveAutonomy={vi.fn().mockResolvedValue(true)}
      />,
    );
    // 關閉時無 USD 上限輸入
    expect(screen.queryByText('settings.autonomy_budget.cost_cap_limit_label')).toBeNull();

    fireEvent.click(screen.getByRole('switch'));
    expect(setAutonomyConfig).toHaveBeenCalledWith(
      expect.objectContaining({ cap: { enabled: true, max_total_cost_usd: null } }),
    );
  });

  it('cap 啟用時顯示 USD 輸入；修改數值會回呼新的 max_total_cost_usd', () => {
    const setAutonomyConfig = vi.fn();
    render(
      <AutonomyBudget
        autonomyConfig={makeConfig({ enabled: true, max_total_cost_usd: 5 })}
        setAutonomyConfig={setAutonomyConfig}
        isSavingAutonomy={false}
        saveAutonomy={vi.fn().mockResolvedValue(true)}
      />,
    );
    const input = screen.getByRole('spinbutton') as HTMLInputElement;
    expect(input.value).toBe('5');

    fireEvent.change(input, { target: { value: '12.5' } });
    expect(setAutonomyConfig).toHaveBeenCalledWith(
      expect.objectContaining({ cap: { enabled: true, max_total_cost_usd: 12.5 } }),
    );
  });
});

describe('AutonomyBudget 工具輪保險絲（ADR-0146）', () => {
  it('max_tool_rounds=0 顯示「不限」；拉動滑桿只寫 nested 新鍵', () => {
    const setAutonomyConfig = vi.fn();
    render(
      <AutonomyBudget
        autonomyConfig={makeConfig()}
        setAutonomyConfig={setAutonomyConfig}
        isSavingAutonomy={false}
        saveAutonomy={vi.fn().mockResolvedValue(true)}
      />,
    );
    expect(screen.getByText('settings.autonomy_budget.unlimited')).toBeDefined();

    const sliders = screen.getAllByRole('slider');
    fireEvent.change(sliders[0], { target: { value: '100' } });
    expect(setAutonomyConfig).toHaveBeenCalled();
    const callArg = setAutonomyConfig.mock.calls[0][0];
    expect(callArg.react_loop).toEqual({ max_tool_rounds: 100 });
    expect('max_react_slots' in callArg).toBe(false);
  });
});
