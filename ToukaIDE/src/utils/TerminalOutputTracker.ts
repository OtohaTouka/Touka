import * as vscode from 'vscode';
import { terminalOutputStore } from './TerminalOutputStore';

const NO_OUTPUT_MSG = '(No terminal output captured yet — run a command first)';

/**
 * Facade that exposes terminal output to the @terminal mention handler.
 *
 * Output is written by TerminalHandlers after each execute_terminal call
 * (via terminalOutputStore.append), so this tracker just reads from the store.
 * This avoids the proposed VS Code API vscode.window.onDidWriteTerminalData.
 */
export class TerminalOutputTracker implements vscode.Disposable {
    track(): void {
        // No-op: output is populated by TerminalHandlers via terminalOutputStore.
    }

    getLatestOutput(): string {
        const output = terminalOutputStore.getLatestOutput();
        return output || NO_OUTPUT_MSG;
    }

    dispose(): void {
        // Nothing to clean up.
    }
}

export const terminalOutputTracker = new TerminalOutputTracker();
