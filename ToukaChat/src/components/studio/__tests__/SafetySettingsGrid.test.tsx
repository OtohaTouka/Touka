import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SafetySettingsGrid } from '../SafetySettingsGrid';

describe('SafetySettingsGrid (shared)', () => {
  it('renders all four harm categories with a full threshold row each', () => {
    render(<SafetySettingsGrid value={[]} onChange={() => {}} />);

    expect(screen.getByText(/騷擾訊息/)).toBeInTheDocument();
    expect(screen.getByText(/仇恨言論/)).toBeInTheDocument();
    expect(screen.getByText(/色情內容/)).toBeInTheDocument();
    expect(screen.getByText(/危險行為/)).toBeInTheDocument();

    // 4 categories × 4 thresholds = 16 threshold buttons.
    expect(screen.getAllByRole('button')).toHaveLength(16);
  });

  it('emits onChange with the chosen threshold for the clicked category', () => {
    const onChange = vi.fn();
    render(<SafetySettingsGrid value={[]} onChange={onChange} />);

    // First "不封鎖" (BLOCK_NONE) button belongs to the first category (harassment).
    fireEvent.click(screen.getAllByText('不封鎖')[0]);

    expect(onChange).toHaveBeenCalledWith([
      { category: 'HARM_CATEGORY_HARASSMENT', threshold: 'BLOCK_NONE' },
    ]);
  });

  it('marks an existing non-default threshold as active', () => {
    render(
      <SafetySettingsGrid
        value={[{ category: 'HARM_CATEGORY_HARASSMENT', threshold: 'BLOCK_NONE' }]}
        onChange={() => {}}
      />,
    );

    // Harassment's "不封鎖" button carries the active styling.
    expect(screen.getAllByText('不封鎖')[0].className).toContain('bg-primary-button');
  });
});
