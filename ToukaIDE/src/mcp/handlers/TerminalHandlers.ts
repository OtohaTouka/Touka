import { spawn } from 'node:child_process';

import * as vscode from 'vscode';

import { terminalOutputStore } from '../../utils/TerminalOutputStore';
import { AuthLevel, evaluateTerminalPolicy } from '../authPolicy';

export async function handleExecuteTerminal(
    command: string,
    authLevel: AuthLevel,
    askUser: (title: string, detail: string) => Promise<boolean>,
    signal?: AbortSignal,
): Promise<string> {
    const decision = evaluateTerminalPolicy(command, authLevel);
    if (decision === 'deny') {
        return `❌ Blacklisted command denied (ToukaIDE never runs it): \`${command}\``;
    }
    if (
        decision === 'confirm'
        && !await askUser(
            '執行終端機指令',
            `Touka 請求執行指令: \`${command}\` (授權等級: ${authLevel})`,
        )
    ) {
        return 'User denied terminal execution permission.';
    }

    if (signal?.aborted) {
        return 'Terminal execution cancelled.';
    }

    // Run exactly once (ADR-0246 U2c-7): this captured child process is the only
    // execution; also sending the command to the integrated terminal ran it twice.
    return new Promise((resolveResult) => {
        let stdout = '';
        let stderr = '';
        let settled = false;
        const timeoutMs = 45_000;
        const cwd = vscode.workspace.workspaceFolders?.[0].uri.fsPath || process.cwd();
        const isWindows = process.platform === 'win32';
        const shell = isWindows ? 'powershell.exe' : '/bin/bash';
        const args = isWindows
            ? ['-NoProfile', '-Command', command]
            : ['-c', command];
        const processHandle = spawn(shell, args, { cwd });

        const finish = (result: string): void => {
            if (settled) {
                return;
            }
            settled = true;
            clearTimeout(timer);
            signal?.removeEventListener('abort', cancel);
            resolveResult(result);
        };
        const cancel = (): void => {
            processHandle.kill();
            finish('Terminal execution cancelled.');
        };
        const timer = setTimeout(() => {
            processHandle.kill();
            finish(
                `⚠️ 指令執行逾時 (${timeoutMs / 1000}s)。\n`
                + `[STDOUT]\n${stdout}\n[STDERR]\n${stderr}\n[EXIT_CODE: -1]`,
            );
        }, timeoutMs);
        signal?.addEventListener('abort', cancel, { once: true });

        processHandle.stdout.on('data', (data: Buffer) => {
            stdout += data.toString();
        });
        processHandle.stderr.on('data', (data: Buffer) => {
            stderr += data.toString();
        });
        processHandle.on('close', (code) => {
            const result = `[STDOUT]\n${stdout || '(無輸出)'}\n\n`
                + `[STDERR]\n${stderr || '(無錯誤訊息)'}\n\n`
                + `[EXIT_CODE: ${code ?? 0}]`;
            terminalOutputStore.append(command.slice(0, 40), result);
            finish(result);
        });
        processHandle.on('error', (error) => {
            finish(`❌ 無法啟動指令執行器: ${error.message}\n[EXIT_CODE: -1]`);
        });
    });
}
