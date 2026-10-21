/** IDE modes (ADR-0246 U2c-7): the IDE has no reviewer, so only Manual and YOLO. */
export enum AuthLevel {
    Manual = 'Manual',
    YOLO = 'YOLO',
}

export type TerminalPolicyDecision = 'deny' | 'confirm' | 'allow';

const TERMINAL_BLACKLIST = [
    /git\s+.*(main|master)/,
    /git\s+checkout\b/,
    /git\s+push\b/,
    /git\s+reset\b/,
    /docker\s+(restart|stop|rm)/,
    /docker-compose\s+(down|stop|restart)/,
    /rm\s+-rf/,
    /format\b/,
    /drop\s+table/,
] as const;

export function isTerminalCommandBlacklisted(command: string): boolean {
    const normalized = command.toLowerCase();
    return TERMINAL_BLACKLIST.some((pattern) => pattern.test(normalized));
}

/** Blacklisted commands are refused in every mode; otherwise YOLO runs and Manual asks. */
export function evaluateTerminalPolicy(
    command: string,
    authLevel: AuthLevel,
): TerminalPolicyDecision {
    if (isTerminalCommandBlacklisted(command)) {
        return 'deny';
    }
    return authLevel === AuthLevel.YOLO ? 'allow' : 'confirm';
}

/**
 * Whether the IDE must ask the human itself (modal or diff review). Only YOLO or a
 * verified "approved by a human on the Touka card" marker skips it.
 */
export function needsIdeConfirmation(
    authLevel: AuthLevel,
    humanApproved: boolean,
): boolean {
    return authLevel !== AuthLevel.YOLO && !humanApproved;
}
