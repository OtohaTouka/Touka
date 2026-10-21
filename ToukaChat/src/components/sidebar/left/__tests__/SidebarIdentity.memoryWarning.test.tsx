import React from 'react';
import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SidebarIdentity } from '../SidebarIdentity';
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
afterEach(cleanup);
const MARKER = 'sidebar.memory_notice.warning_marker';
describe('memory warning marker', () => {
    it('shows a labelled marker with a tooltip when the memory system has a warning', () => {
        render(<SidebarIdentity mode="hybrid" checkState="ready" memoryWarning />);
        const marker = screen.getByRole('img', { name: MARKER });
        expect(marker).toHaveAttribute('title', MARKER);
    });
    it.each([false, undefined])('shows no marker when memoryWarning is %s', memoryWarning => {
        render(<SidebarIdentity mode="hybrid" checkState="ready" memoryWarning={memoryWarning} />);
        expect(screen.queryByRole('img', { name: MARKER })).toBeNull();
    });
});
