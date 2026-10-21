import { describe, it, expect } from 'vitest';
import { THEMES, PRIMITIVES, AGENT_TOKENS } from '@/lib/design-system/tokens';

describe('Design Tokens System (SSOT)', () => {
    it('should have a consistent primary theme (Touka)', () => {
        expect(THEMES.touka).toBeDefined();
        expect(THEMES.touka.primary).toBe(PRIMITIVES.touka[600]);
    });

    it('should define core agent identity colors', () => {
        expect(AGENT_TOKENS.Amber.primary).toBe(PRIMITIVES.amber[500]);
        expect(AGENT_TOKENS.Rose.primary).toBe(PRIMITIVES.rose[500]);
        expect(AGENT_TOKENS.Emerald.primary).toBe(PRIMITIVES.emerald[500]);
    });

    it('should ensure all themes have a background and surface color', () => {
        Object.values(THEMES).forEach(theme => {
            expect(theme.bg).toMatch(/^#|rgba/);
            expect(theme.surface).toMatch(/^#|rgba/);
        });
    });
});







