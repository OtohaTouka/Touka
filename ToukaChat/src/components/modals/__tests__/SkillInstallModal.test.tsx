import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import SkillInstallModal from '../SkillInstallModal';

describe('SkillInstallModal', () => {
    it('renders URL input and install button', () => {
        render(<SkillInstallModal onConfirm={vi.fn()} onCancel={vi.fn()} />);
        expect(screen.getByPlaceholderText(/\.skill/i)).toBeDefined();
        expect(screen.getByText('安裝')).toBeDefined();
    });

    it('calls onConfirm with the entered URL', async () => {
        const onConfirm = vi.fn(() => Promise.resolve(true));
        render(<SkillInstallModal onConfirm={onConfirm} onCancel={vi.fn()} />);
        fireEvent.change(screen.getByPlaceholderText(/\.skill/i), {
            target: { value: 'https://github.com/x/my.skill' },
        });
        fireEvent.click(screen.getByText('安裝'));
        await waitFor(() => {
            expect(onConfirm).toHaveBeenCalledWith('https://github.com/x/my.skill');
        });
    });

    it('calls onCancel when clicking cancel', () => {
        const onCancel = vi.fn();
        render(<SkillInstallModal onConfirm={vi.fn()} onCancel={onCancel} />);
        fireEvent.click(screen.getByText('取消'));
        expect(onCancel).toHaveBeenCalled();
    });

    it('disables install button when URL is empty', () => {
        render(<SkillInstallModal onConfirm={vi.fn()} onCancel={vi.fn()} />);
        const installBtn = screen.getByText('安裝') as HTMLButtonElement;
        expect(installBtn.disabled).toBe(true);
    });
});
