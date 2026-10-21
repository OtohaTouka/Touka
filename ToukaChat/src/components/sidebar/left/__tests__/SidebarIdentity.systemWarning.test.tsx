import React from 'react';
import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SidebarIdentity } from '../SidebarIdentity';
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
afterEach(cleanup);
const SYSTEM = 'sidebar.system_notice.warning_marker';
const MEMORY = 'sidebar.memory_notice.warning_marker';
describe('system warning marker', () => {
    it('shows the system marker, which outranks a memory warning', () => {
        render(<SidebarIdentity mode="hybrid" checkState="ready" memoryWarning systemWarning />);
        expect(screen.getByRole('img', { name: SYSTEM })).toHaveAttribute('title', SYSTEM);
        expect(screen.queryByRole('img', { name: MEMORY })).toBeNull();
    });
    it('shows no system marker without a system warning', () => {
        render(<SidebarIdentity mode="hybrid" checkState="ready" />);
        expect(screen.queryByRole('img', { name: SYSTEM })).toBeNull();
    });
});
