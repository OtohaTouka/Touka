import React from 'react';
import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SidebarIdentity } from '../SidebarIdentity';
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
afterEach(cleanup);
describe('capability badge', () => {
    it.each(['checking', 'auth_required', 'forbidden', 'unavailable'])('does not label %s Standalone', checkState => {
        render(<SidebarIdentity mode="unknown" checkState={checkState} />);
        expect(screen.queryByText('Standalone')).not.toBeInTheDocument();
        expect(screen.getByText(`sidebar.system_status.${checkState}`)).toBeInTheDocument();
    });
    it.each(['standalone', 'hybrid'])('renders confirmed %s through i18n', mode => {
        render(<SidebarIdentity mode={mode} checkState="ready" />);
        expect(screen.getByText(`sidebar.system_status.${mode}`)).toBeInTheDocument();
    });
});
