import { describe, it, expect } from 'vitest';
import { generateAuthToken } from '../utils/authToken';

describe('generateAuthToken', () => {
    it('returns a non-empty string', () => {
        expect(generateAuthToken()).toBeTruthy();
    });

    it('returns a hex string of 64 characters (32 bytes)', () => {
        const token = generateAuthToken();
        expect(token).toMatch(/^[0-9a-f]{64}$/);
    });

    it('returns a different token each call (non-deterministic)', () => {
        const tokens = new Set(Array.from({ length: 10 }, () => generateAuthToken()));
        expect(tokens.size).toBe(10);
    });
});
