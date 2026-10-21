/** Step5DataPath a11y（#4 批次 4）：label 與 input 正確關聯（htmlFor/id）。 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { Step5DataPath } from '../steps/Step5DataPath';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

describe('Step5DataPath a11y', () => {
  it('資料目錄路徑 label 與 input 正確關聯', () => {
    render(<Step5DataPath status={null} onNext={vi.fn()} />);
    expect(screen.getByLabelText(/資料目錄路徑/)).toBeInTheDocument();
  });
});
