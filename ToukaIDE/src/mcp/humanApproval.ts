import { createHash, createHmac, timingSafeEqual } from 'node:crypto';

/**
 * "Approved by a human on the Touka card" marker (ADR-0246 U2c-7). Touka puts it in
 * the tools/call `_meta` only after the human approved that exact call; the IDE then
 * skips its own second confirmation. Any failure means "ask as usual".
 */
export const HUMAN_APPROVAL_META_KEY = 'touka.dev/human-approval';
/** Touka mints 60 s markers; the extra minute only tolerates clock skew. */
const MAX_TTL_MS = 120_000;
const MAC_DOMAIN = 'touka-ide-human-approval/v1';
const NONCE_PATTERN = /^[0-9a-f]{32}$/;
const MAC_PATTERN = /^[0-9a-f]{64}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Same bytes as Touka's `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. */
export function canonicalJson(value: unknown): string {
    if (Array.isArray(value)) {
        return `[${value.map(canonicalJson).join(',')}]`;
    }
    if (isRecord(value)) {
        const entries = Object.keys(value).sort()
            .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`);
        return `{${entries.join(',')}}`;
    }
    return JSON.stringify(value);
}

export class HumanApprovalVerifier {
    private readonly usedNonces = new Map<string, number>();

    /** `key` is this session's MCP bearer, which only Touka got from the discovery file. */
    public constructor(
        private readonly key: string,
        private readonly now: () => number = Date.now,
    ) {}

    /** True once for a fresh marker MAC'd with this session's bearer over exactly this call. */
    public verify(meta: unknown, toolName: string, args: Record<string, unknown>): boolean {
        const marker = isRecord(meta) ? meta[HUMAN_APPROVAL_META_KEY] : undefined;
        if (!isRecord(marker) || marker.v !== 1) {
            return false;
        }
        const { nonce, exp, mac } = marker;
        const now = this.now();
        if (
            typeof nonce !== 'string' || !NONCE_PATTERN.test(nonce)
            || typeof mac !== 'string' || !MAC_PATTERN.test(mac)
            || typeof exp !== 'number' || !Number.isSafeInteger(exp)
            || exp <= now || exp > now + MAX_TTL_MS
        ) {
            return false;
        }
        for (const [used, usedExp] of this.usedNonces) {
            if (usedExp <= now) {
                this.usedNonces.delete(used);
            }
        }
        if (this.usedNonces.has(nonce)) {
            return false;
        }
        const digest = createHash('sha256').update(canonicalJson(args), 'utf8').digest('hex');
        const expected = createHmac('sha256', this.key)
            .update([MAC_DOMAIN, nonce, String(exp), toolName, digest].join('\n'), 'utf8')
            .digest();
        if (!timingSafeEqual(expected, Buffer.from(mac, 'hex'))) {
            return false;
        }
        this.usedNonces.set(nonce, exp);
        return true;
    }
}
