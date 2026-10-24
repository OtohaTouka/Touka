import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import { SoulToolPicker } from '../SoulToolPicker';
import type { SoulToolEntry } from '@/lib/soul_tools';

const TOOLS: SoulToolEntry[] = [
    { key: 'read', label: 'read', description: '讀檔' },
    { key: 'srv/lookup', label: 'srv/lookup', description: '查', legacyKey: 'srv__lookup' },
];

describe('SoulToolPicker', () => {
    it('ticks a tool by saving its server/tool key', () => {
        const onChange = vi.fn();
        render(<SoulToolPicker tools={TOOLS} value={['read']} onChange={onChange} />);

        expect(screen.getByRole('button', { name: 'read' })).toHaveAttribute('aria-pressed', 'true');
        fireEvent.click(screen.getByRole('button', { name: 'srv/lookup' }));

        expect(onChange).toHaveBeenCalledWith(['read', 'srv/lookup']);
    });

    it('keeps and marks entries that are not listed right now; removing one is explicit', () => {
        const onChange = vi.fn();
        render(<SoulToolPicker tools={TOOLS} value={['gone/tool', 'srv__lookup']} onChange={onChange} />);

        expect(screen.getByRole('button', { name: 'srv/lookup' })).toHaveAttribute('aria-pressed', 'true');
        const gone = screen.getByRole('button', { name: /gone\/tool/ });
        expect(gone).toHaveTextContent('目前不在清單');

        fireEvent.click(gone);
        expect(onChange).toHaveBeenCalledWith(['srv__lookup']);
    });

    it('counts legacy short names that need ticking again', () => {
        render(<SoulToolPicker tools={TOOLS} value={['lookup', 'search', 'gone/tool']} onChange={vi.fn()} />);

        expect(screen.getByText('有 2 個工具名稱需要重新勾選')).toBeInTheDocument();
    });

    it('read-only shows only what is ticked, without buttons', () => {
        render(<SoulToolPicker tools={TOOLS} value={['srv__lookup', 'gone/tool']} />);

        expect(screen.queryByRole('button')).not.toBeInTheDocument();
        expect(screen.getByText('srv/lookup')).toBeInTheDocument();
        expect(screen.queryByText('read')).not.toBeInTheDocument();
        expect(screen.getByText(/gone\/tool/)).toHaveTextContent('目前不在清單');
    });
});
