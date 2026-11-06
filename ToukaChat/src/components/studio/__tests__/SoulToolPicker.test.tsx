import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import { SoulToolPicker } from '../SoulToolPicker';
import type { SoulToolCatalog } from '@/lib/soul_tools';

const READY: SoulToolCatalog = {
    status: 'ready',
    tools: [
        { key: 'read', label: 'read', description: '讀檔' },
        { key: 'execute_command', label: 'execute_command', description: '' },
        { key: 'stop_command', label: 'stop_command', description: '' },
        { key: 'srv/lookup', label: 'srv/lookup', description: '查', legacyKey: 'srv__lookup' },
    ],
    companions: { execute_command: ['stop_command'] },
};

describe('SoulToolPicker', () => {
    it('ticks a tool by saving its server/tool key', () => {
        const onChange = vi.fn();
        render(<SoulToolPicker catalog={READY} value={['read']} onChange={onChange} />);

        expect(screen.getByRole('button', { name: 'read' })).toHaveAttribute('aria-pressed', 'true');
        fireEvent.click(screen.getByRole('button', { name: 'srv/lookup' }));

        expect(onChange).toHaveBeenCalledWith(['read', 'srv/lookup']);
    });

    it('keeps and marks entries that are not listed right now; removing one is explicit', () => {
        const onChange = vi.fn();
        render(<SoulToolPicker catalog={READY} value={['gone/tool', 'srv__lookup']} onChange={onChange} />);

        expect(screen.getByRole('button', { name: 'srv/lookup' })).toHaveAttribute('aria-pressed', 'true');
        const gone = screen.getByRole('button', { name: /gone\/tool/ });
        expect(gone).toHaveTextContent('目前不在清單');

        fireEvent.click(gone);
        expect(onChange).toHaveBeenCalledWith(['srv__lookup']);
    });

    it('counts names that match nothing and says they can be removed', () => {
        render(<SoulToolPicker catalog={READY} value={['lookup', 'search', 'gone/tool']} onChange={vi.fn()} />);

        expect(screen.getByText(/有 2 個工具名稱對不到任何工具/)).toBeInTheDocument();
    });

    it('read-only shows only what is ticked, without buttons', () => {
        render(<SoulToolPicker catalog={READY} value={['srv__lookup', 'gone/tool']} />);

        expect(screen.queryByRole('button')).not.toBeInTheDocument();
        expect(screen.getByText('srv/lookup')).toBeInTheDocument();
        expect(screen.queryByText('read')).not.toBeInTheDocument();
        expect(screen.getByText(/gone\/tool/)).toHaveTextContent('目前不在清單');
    });

    it('shows companions as ticked along with execute_command, not toggleable on their own', () => {
        const onChange = vi.fn();
        render(<SoulToolPicker catalog={READY} value={['execute_command']} onChange={onChange} />);

        const companion = screen.getByRole('button', { name: /stop_command/ });
        expect(companion).toHaveAttribute('aria-pressed', 'true');
        expect(companion).toBeDisabled();
        expect(companion).toHaveAttribute('title', '隨 execute_command 一起開');
    });

    it.each(['loading', 'failed'] as const)('while %s it marks nothing as unlisted and cannot edit', status => {
        const onChange = vi.fn();
        render(
            <SoulToolPicker catalog={{ status, tools: [], companions: {} }} value={['read', 'lookup']} onChange={onChange} />,
        );

        expect(screen.queryByRole('button')).not.toBeInTheDocument();
        expect(screen.getByText('read')).toBeInTheDocument();
        expect(screen.queryByText(/目前不在清單/)).not.toBeInTheDocument();
        expect(screen.queryByText(/對不到任何工具/)).not.toBeInTheDocument();
        expect(screen.getByText(status === 'loading' ? '正在讀取工具清單…' : '讀不到工具清單：重新開啟這個畫面再試一次')).toBeInTheDocument();
    });

    it('an empty list still says every tool is available while the list cannot be read', () => {
        render(<SoulToolPicker catalog={{ status: 'failed', tools: [], companions: {} }} value={[]} onChange={vi.fn()} />);

        expect(screen.getByText('不勾任何工具代表全部可用')).toHaveClass('text-status-warning');
    });

    it('does not crash on names that are built into every JavaScript object', () => {
        render(<SoulToolPicker catalog={READY} value={['constructor', 'toString', 'execute_command']} onChange={vi.fn()} />);

        expect(screen.getByRole('button', { name: /stop_command/ })).toHaveAttribute('title', '隨 execute_command 一起開');
        expect(screen.getByRole('button', { name: /constructor/ })).toHaveTextContent('目前不在清單');
    });

    it('an empty list says every tool is available, louder while editing', () => {
        const { rerender } = render(<SoulToolPicker catalog={READY} value={[]} />);
        expect(screen.getByText('不勾任何工具代表全部可用')).toHaveClass('text-muted-foreground');

        rerender(<SoulToolPicker catalog={READY} value={[]} onChange={vi.fn()} />);
        expect(screen.getByText('不勾任何工具代表全部可用')).toHaveClass('text-status-warning');
    });
});
