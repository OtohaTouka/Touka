import { describe, expect, it } from 'vitest';

import {
    AuthLevel,
    evaluateTerminalPolicy,
    needsIdeConfirmation,
} from '../mcp/authPolicy';

describe('ToukaIDE modes (ADR-0246 U2c-7)', () => {
    it('keeps only Manual and YOLO', () => {
        expect(Object.values(AuthLevel)).toEqual(['Manual', 'YOLO']);
    });

    it('asks for every non-blacklisted command in Manual, including the old SafeAuto list', () => {
        for (const command of ['npm test', 'npm install', 'ls', 'cat a.txt', 'curl https://x.test']) {
            expect(evaluateTerminalPolicy(command, AuthLevel.Manual)).toBe('confirm');
        }
    });

    it('runs non-blacklisted commands in YOLO', () => {
        expect(evaluateTerminalPolicy('node --version', AuthLevel.YOLO)).toBe('allow');
        expect(evaluateTerminalPolicy('curl https://x.test', AuthLevel.YOLO)).toBe('allow');
    });

    it('denies blacklisted commands outright in every mode', () => {
        for (const level of [AuthLevel.Manual, AuthLevel.YOLO]) {
            expect(evaluateTerminalPolicy('git push origin main', level)).toBe('deny');
            expect(evaluateTerminalPolicy('docker stop touka-proxy', level)).toBe('deny');
            expect(evaluateTerminalPolicy('RM -RF build', level)).toBe('deny');
        }
    });

    it('skips the IDE confirmation only in YOLO or for a verified human approval', () => {
        expect(needsIdeConfirmation(AuthLevel.Manual, false)).toBe(true);
        expect(needsIdeConfirmation(AuthLevel.Manual, true)).toBe(false);
        expect(needsIdeConfirmation(AuthLevel.YOLO, false)).toBe(false);
        expect(needsIdeConfirmation(AuthLevel.YOLO, true)).toBe(false);
    });
});

// Not-wider table: old IDE levels (literal results before U2c-7) vs the two modes.
// 0 = refused, 1 = the IDE asks the human, 2 = runs without asking.
const ACTIONS = ['npm_test', 'curl', 'blacklisted', 'write_file', 'git_commit'] as const;
type Action = typeof ACTIONS[number];
const COMMANDS: Partial<Record<Action, string>> = {
    npm_test: 'npm test',
    curl: 'curl https://x.test',
    blacklisted: 'git push origin main',
};
const OLD: Record<string, Record<Action, number>> = {
    ReadOnly: { npm_test: 0, curl: 0, blacklisted: 0, write_file: 0, git_commit: 0 },
    Manual: { npm_test: 1, curl: 1, blacklisted: 1, write_file: 1, git_commit: 1 },
    SafeAuto: { npm_test: 2, curl: 1, blacklisted: 1, write_file: 2, git_commit: 1 },
    FullYOLO: { npm_test: 2, curl: 2, blacklisted: 1, write_file: 2, git_commit: 2 },
};
const PAIRS: [string, AuthLevel][] = [
    ['ReadOnly', AuthLevel.Manual],
    ['Manual', AuthLevel.Manual],
    ['SafeAuto', AuthLevel.Manual],
    ['FullYOLO', AuthLevel.YOLO],
];

function outcome(level: AuthLevel, humanApproved: boolean, action: Action): number {
    const command = COMMANDS[action];
    if (command !== undefined) {
        const decision = evaluateTerminalPolicy(command, level);
        if (decision === 'deny') {
            return 0;
        }
        if (decision === 'allow') {
            return 2;
        }
    }
    return needsIdeConfirmation(level, humanApproved) ? 1 : 2;
}

function widenedCells(): string[] {
    const cells: string[] = [];
    for (const [old, level] of PAIRS) {
        for (const humanApproved of [false, true]) {
            for (const action of ACTIONS) {
                if (outcome(level, humanApproved, action) > OLD[old][action]) {
                    cells.push(`${old}>${level}:${humanApproved ? 'approved' : 'plain'}:${action}`);
                }
            }
        }
    }
    return cells.sort();
}

describe('ToukaIDE not-wider table (ADR-0246 U2c-7)', () => {
    it('widens exactly the pre-approved cells', () => {
        // ③ ReadOnly→Manual (ask, then run); decision 9 甲: a human approval on the
        // Touka card skips the second IDE prompt. Nothing else may widen.
        const approved = [
            ...['npm_test', 'curl', 'write_file', 'git_commit'].flatMap((action) => [
                `ReadOnly>Manual:plain:${action}`,
                `ReadOnly>Manual:approved:${action}`,
                `Manual>Manual:approved:${action}`,
            ]),
            'SafeAuto>Manual:approved:curl',
            'SafeAuto>Manual:approved:git_commit',
        ].sort();
        expect(widenedCells()).toEqual(approved);
    });

    it('is strictly narrower for blacklisted commands and old SafeAuto auto-runs', () => {
        expect(outcome(AuthLevel.YOLO, true, 'blacklisted')).toBe(0);
        expect(outcome(AuthLevel.Manual, true, 'blacklisted')).toBe(0);
        expect(outcome(AuthLevel.Manual, false, 'npm_test')).toBe(1);
        expect(outcome(AuthLevel.Manual, false, 'write_file')).toBe(1);
    });
});
