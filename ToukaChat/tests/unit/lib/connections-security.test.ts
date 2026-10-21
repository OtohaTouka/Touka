/**
 * Unit tests for M5 security hardening (TDD).
 *
 * M5 — Header Injection: sanitizeHeaderValue() must strip characters that
 * can enable HTTP header injection (\r, \n, \x00) before headers are set.
 */

import { describe, it, expect } from 'vitest';
import { sanitizeHeaderValue } from '@/lib/connections';

describe('sanitizeHeaderValue (M5 fix)', () => {
    it('returns normal alphanumeric values unchanged', () => {
        expect(sanitizeHeaderValue('Alice')).toBe('Alice');
        expect(sanitizeHeaderValue('Agent-1')).toBe('Agent-1');
        expect(sanitizeHeaderValue('Bot_42')).toBe('Bot_42');
    });

    it('strips carriage return characters (CR injection)', () => {
        const evil = 'Alice\rX-Evil-Header: injected';
        const result = sanitizeHeaderValue(evil);
        expect(result).not.toContain('\r');
        expect(result).toBe('AliceX-Evil-Header: injected');
    });

    it('strips newline characters (LF injection)', () => {
        const evil = 'Alice\nX-Evil-Header: injected';
        const result = sanitizeHeaderValue(evil);
        expect(result).not.toContain('\n');
    });

    it('strips null bytes', () => {
        const evil = 'Alice\x00Admin';
        const result = sanitizeHeaderValue(evil);
        expect(result).not.toContain('\x00');
        expect(result).toBe('AliceAdmin');
    });

    it('strips CRLF combination (full header injection vector)', () => {
        const evil = 'Alice\r\nX-Injected: evil';
        const result = sanitizeHeaderValue(evil);
        expect(result).not.toContain('\r');
        expect(result).not.toContain('\n');
    });

    it('handles empty string without error', () => {
        expect(sanitizeHeaderValue('')).toBe('');
    });

    it('handles string with only dangerous characters', () => {
        expect(sanitizeHeaderValue('\r\n\x00')).toBe('');
    });
});
