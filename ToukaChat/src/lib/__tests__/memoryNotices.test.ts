import { describe, expect, it } from 'vitest';
import { hasMemoryWarning } from '../memoryNotices';

describe('hasMemoryWarning', () => {
    it('is true when any memory notice is a warning', () => {
        expect(hasMemoryWarning([{ code: 'a', level: 'info' }, { code: 'b', level: 'warning' }])).toBe(true);
    });
    it('is false for information-only notices', () => {
        expect(hasMemoryWarning([{ code: 'unmigrated_legacy_facts', level: 'info', count: 2 }])).toBe(false);
    });
    it.each([[[]], [undefined]])('is false without notices (%j)', notices => {
        expect(hasMemoryWarning(notices)).toBe(false);
    });
});
