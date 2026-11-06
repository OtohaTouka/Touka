import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryStatus } from '../MemoryStatus';

describe('MemoryStatus — emergence index removed (ADR-0291)', () => {
  it('shows memory and vault health without the emergence index', () => {
    render(
      <MemoryStatus
        memStats={{ memory_load: 4, status: 'Connected' }}
        vaultHealth={{ facts: true, vector: true, legacy: false, personal: true }}
      />,
    );
    expect(screen.getByText('4 nodes')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Vector')).toBeInTheDocument();
    expect(screen.getByText('Legacy')).toBeInTheDocument();
    expect(screen.getByText('Drive')).toBeInTheDocument();
    expect(screen.queryByText(/Emergence/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/E-Index/i)).not.toBeInTheDocument();
  });
});
