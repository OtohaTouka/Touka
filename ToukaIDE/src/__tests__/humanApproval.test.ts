import { describe, expect, it } from 'vitest';

import {
    canonicalJson,
    HUMAN_APPROVAL_META_KEY,
    HumanApprovalVerifier,
} from '../mcp/humanApproval';

// Shared vector with tests/unit/test_ide_human_approval_security.py (Touka mints,
// the IDE verifies). Digest and MAC were computed outside both products.
const KEY = 'u2c7-session-bearer-0123456789abcdef';
const NOW = 1_760_000_000_000;
const TOOL = 'execute_terminal';
const ARGS = {
    command: 'npm test -- --grep "桃花"\n',
    agent_id: 'Alice',
    z: [1, { b: true, a: null }],
};
const MARKER = {
    v: 1,
    nonce: '00112233445566778899aabbccddeeff',
    exp: 1_760_000_060_000,
    mac: 'db039dec16396074c01a7ebca5906c2d4f711defd678aa78aacde6b9e92da4df',
};

function meta(marker: unknown = MARKER): Record<string, unknown> {
    return { [HUMAN_APPROVAL_META_KEY]: marker };
}

function verifier(now: number = NOW, key: string = KEY): HumanApprovalVerifier {
    return new HumanApprovalVerifier(key, () => now);
}

describe('ToukaIDE human-approval marker (ADR-0246 U2c-7)', () => {
    it('uses the touka.dev/human-approval _meta key', () => {
        expect(HUMAN_APPROVAL_META_KEY).toBe('touka.dev/human-approval');
    });

    it('serializes arguments like Touka (sorted keys, compact, raw unicode)', () => {
        expect(canonicalJson(ARGS)).toBe(
            '{"agent_id":"Alice","command":"npm test -- --grep \\"桃花\\"\\n",'
            + '"z":[1,{"a":null,"b":true}]}',
        );
    });

    it('accepts a fresh marker minted with this session bearer for exactly this call', () => {
        expect(verifier().verify(meta(), TOOL, ARGS)).toBe(true);
    });

    it('accepts a marker only once (no replay)', () => {
        const instance = verifier();
        expect(instance.verify(meta(), TOOL, ARGS)).toBe(true);
        expect(instance.verify(meta(), TOOL, ARGS)).toBe(false);
        expect(instance.verify(meta(), TOOL, { ...ARGS })).toBe(false);
    });

    it('rejects a marker moved to another call', () => {
        expect(verifier().verify(meta(), TOOL, { ...ARGS, command: 'git push' })).toBe(false);
        expect(verifier().verify(meta(), 'git_commit', ARGS)).toBe(false);
        expect(verifier().verify(meta(), TOOL, { ...ARGS, extra: 1 })).toBe(false);
    });

    it('rejects a forged marker or one minted with another session bearer', () => {
        const forged = { ...MARKER, mac: `0${MARKER.mac.slice(1)}` };
        expect(verifier().verify(meta(forged), TOOL, ARGS)).toBe(false);
        expect(verifier(NOW, 'another-session-bearer').verify(meta(), TOOL, ARGS)).toBe(false);
        const otherNonce = { ...MARKER, nonce: 'ffeeddccbbaa99887766554433221100' };
        expect(verifier().verify(meta(otherNonce), TOOL, ARGS)).toBe(false);
        const otherExp = { ...MARKER, exp: MARKER.exp - 1 };
        expect(verifier().verify(meta(otherExp), TOOL, ARGS)).toBe(false);
    });

    it('rejects expired markers and markers valid too far into the future', () => {
        expect(verifier(MARKER.exp).verify(meta(), TOOL, ARGS)).toBe(false);
        expect(verifier(MARKER.exp - 120_001).verify(meta(), TOOL, ARGS)).toBe(false);
        expect(verifier(MARKER.exp - 120_000).verify(meta(), TOOL, ARGS)).toBe(true);
    });

    it('rejects missing or malformed markers', () => {
        const instance = verifier();
        const cases: unknown[] = [
            undefined,
            null,
            {},
            { 'touka.dev/other': MARKER },
            meta(null),
            meta('approved'),
            meta({ ...MARKER, v: 2 }),
            meta({ ...MARKER, nonce: MARKER.nonce.toUpperCase() }),
            meta({ ...MARKER, nonce: MARKER.nonce.slice(2) }),
            meta({ ...MARKER, exp: String(MARKER.exp) }),
            meta({ ...MARKER, exp: MARKER.exp + 0.5 }),
            meta({ ...MARKER, mac: MARKER.mac.toUpperCase() }),
            meta({ ...MARKER, mac: MARKER.mac.slice(2) }),
        ];
        for (const value of cases) {
            expect(instance.verify(value, TOOL, ARGS)).toBe(false);
        }
        expect(instance.verify(meta(), TOOL, ARGS)).toBe(true);
    });
});
