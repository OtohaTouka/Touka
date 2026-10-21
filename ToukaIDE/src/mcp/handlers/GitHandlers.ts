import * as vscode from 'vscode';
import { GitExtension, Repository } from '../types';

function getGitRepository(): Repository | undefined {
    const extension = vscode.extensions.getExtension<GitExtension>('vscode.git');
    if (!extension) throw new Error("VS Code Git extension is not available.");
    
    const git = extension.exports.getAPI(1);
    if (git.repositories.length === 0) return undefined;
    return git.repositories[0];
}

export async function handleGetGitChanges(): Promise<string> {
    const repo = getGitRepository();
    if (!repo) return "No Git repository found in the workspace.";

    let summary = "Git Changes Summary:\n";
    const changes = [
        ...repo.state.workingTreeChanges.map(c => `Unstaged: ${c.uri.fsPath}`),
        ...repo.state.indexChanges.map(c => `Staged: ${c.uri.fsPath}`)
    ];

    if (changes.length === 0) return "No changes detected.";

    const diff = await repo.diff(false);
    return summary + changes.join('\n') + "\n\nDiff:\n" + diff;
}

export async function handleGitCommit(
    message: string, 
    askUser: (title: string, detail: string) => Promise<boolean>
): Promise<string> {
    const repo = getGitRepository();
    if (!repo) return "No Git repository found.";

    const permission = await askUser(
        "Git Commit", 
        `Touka 請求提交所有變更: "${message}"`
    );

    if (!permission) return "User denied Git commit permission.";

    try {
        await repo.commit(message, { all: true });
        return `Successfully committed changes with message: "${message}"`;
    } catch (error: any) {
        return `Git commit failed: ${error.message}`;
    }
}
