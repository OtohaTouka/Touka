import { EventEmitter } from 'node:events';

import { afterEach, describe, expect, it, vi } from 'vitest';

const { sendText, spawn } = vi.hoisted(() => ({
    sendText: vi.fn(),
    spawn: vi.fn(),
}));

vi.mock('vscode', () => {
    const terminal = { sendText, show: vi.fn() };
    return {
        window: { activeTerminal: terminal, createTerminal: vi.fn(() => terminal) },
        workspace: { workspaceFolders: undefined },
    };
});
vi.mock('node:child_process', () => ({ spawn }));

import { AuthLevel } from '../mcp/authPolicy';
import { handleExecuteTerminal } from '../mcp/handlers/TerminalHandlers';

function fakeProcess(): EventEmitter & { stdout: EventEmitter; stderr: EventEmitter; kill(): void } {
    const process = Object.assign(new EventEmitter(), {
        stdout: new EventEmitter(),
        stderr: new EventEmitter(),
        kill: vi.fn(),
    });
    queueMicrotask(() => {
        process.stdout.emit('data', Buffer.from('ok'));
        process.emit('close', 0);
    });
    return process;
}

afterEach(() => {
    sendText.mockReset();
    spawn.mockReset();
});

describe('ToukaIDE terminal executor (ADR-0246 U2c-7)', () => {
    it('runs an approved command exactly once', async () => {
        spawn.mockImplementation(fakeProcess);
        const askUser = vi.fn(async () => true);

        const result = await handleExecuteTerminal('git commit -m x', AuthLevel.Manual, askUser);

        expect(askUser).toHaveBeenCalledTimes(1);
        expect(spawn).toHaveBeenCalledTimes(1);
        expect(spawn.mock.calls[0][1]).toContain('git commit -m x');
        expect(sendText).not.toHaveBeenCalled();
        expect(result).toContain('[EXIT_CODE: 0]');
    });

    it('runs a YOLO command exactly once without asking', async () => {
        spawn.mockImplementation(fakeProcess);
        const askUser = vi.fn(async () => true);

        await handleExecuteTerminal('npm install', AuthLevel.YOLO, askUser);

        expect(askUser).not.toHaveBeenCalled();
        expect(spawn).toHaveBeenCalledTimes(1);
        expect(sendText).not.toHaveBeenCalled();
    });

    it('never runs a refused or blacklisted command', async () => {
        const refuse = vi.fn(async () => false);
        await handleExecuteTerminal('npm test', AuthLevel.Manual, refuse);
        const approve = vi.fn(async () => true);
        const result = await handleExecuteTerminal('git push origin main', AuthLevel.YOLO, approve);

        expect(approve).not.toHaveBeenCalled();
        expect(result).toContain('git push origin main');
        expect(spawn).not.toHaveBeenCalled();
        expect(sendText).not.toHaveBeenCalled();
    });
});
