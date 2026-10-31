import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

import { readFileNoFollow, writeFileNoFollow } from './noFollowFile';

export class DiffManager {
    constructor() {}

    /**
     * Proposes changes to a file by opening a native VS Code diff view.
     * @param originalPath The absolute path of the file to modify.
     * @param newContent The new content to write.
     * @param recheck Resolves the requested path again when the user accepts; it must still be `originalPath`.
     * @returns A promise that resolves with a success or rejection message.
     */
    public async proposeChanges(originalPath: string, newContent: string, recheck: () => string): Promise<string> {
        return new Promise(async (resolve) => {
            let tempDir: string | undefined;
            try {
                const originalUri = vscode.Uri.file(originalPath);
                // Read the original without following links, so a FIFO or a link swapped in cannot hang or redirect it (ADR-0294)
                const originalContent = await readFileNoFollow(originalPath).catch((error: NodeJS.ErrnoException) => {
                    if (error.code === 'ENOENT') {
                        return undefined;
                    }
                    throw error;
                });
                const fileExists = originalContent !== undefined;

                // If content is exactly the same, no need to diff
                if ((originalContent ?? '') === newContent) {
                    return resolve("No changes detected. The new content is identical to the existing file.");
                }

                // The proposed content goes in a private folder (mkdtemp creates it 0700), not the shared temp folder (ADR-0294)
                const folder = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'touka-diff-'));
                tempDir = folder;
                const fileName = path.basename(originalPath);
                const ext = path.extname(originalPath);
                const baseNameWithoutExt = path.basename(originalPath, ext);
                const tempFilePath = path.join(folder, `${baseNameWithoutExt}.touka-diff${ext}`);

                await fs.promises.writeFile(tempFilePath, newContent, 'utf8');
                const tempUri = vscode.Uri.file(tempFilePath);

                // Open the native diff view
                // Left side: original, Right side: temp (new content)
                const title = `Touka AI Review: ${fileName}`;
                await vscode.commands.executeCommand('vscode.diff', originalUri, tempUri, title, { preview: true });

                // Show notification with action buttons
                const message = fileExists ? `Touka proposes modifications to ${fileName}.` : `Touka proposes creating a new file: ${fileName}.`;
                
                vscode.window.showInformationMessage(
                    message,
                    { modal: false },
                    "Accept (Apply)", 
                    "Reject (Discard)"
                ).then(async (selection) => {
                    // User rejected or dismissed the notification unless they chose Accept
                    let outcome = `Rejected: User denied the proposed changes to ${fileName}.`;
                    if (selection === "Accept (Apply)") {
                        try {
                            // The path may have changed while the user was reading the diff: check it again
                            if (recheck() !== originalPath) {
                                throw new Error('The path changed after the proposal');
                            }
                            await fs.promises.mkdir(path.dirname(originalPath), { recursive: true });
                            await writeFileNoFollow(originalPath, newContent);
                            outcome = `Success: User accepted and applied the changes to ${fileName}.`;
                        } catch (err: any) {
                            outcome = `Error applying changes: ${err.message}`;
                        }
                    }
                    await this.cleanupTempDir(folder);
                    this.closeDiffEditor(tempUri);
                    resolve(outcome);
                });

            } catch (error: any) {
                if (tempDir) {
                    await this.cleanupTempDir(tempDir);
                }
                resolve(`Error initiating diff: ${error.message}`);
            }
        });
    }

    private async cleanupTempDir(tempDir: string) {
        try {
            await fs.promises.rm(tempDir, { recursive: true, force: true });
        } catch (e) {
            console.error(`[ToukaIDE] Failed to cleanup temp folder: ${tempDir}`, e);
        }
    }

    private closeDiffEditor(tempUri: vscode.Uri) {
        // Attempt to close the active editor if it matches the temp URI
        const activeEditor = vscode.window.activeTextEditor;
        if (activeEditor && activeEditor.document.uri.fsPath === tempUri.fsPath) {
            vscode.commands.executeCommand('workbench.action.closeActiveEditor');
        } else {
            // More aggressive approach: try to find and close it among all tabs
            for (const tabGroup of vscode.window.tabGroups.all) {
                for (const tab of tabGroup.tabs) {
                    if (tab.input instanceof vscode.TabInputTextDiff) {
                        if (tab.input.modified.fsPath === tempUri.fsPath) {
                            vscode.window.tabGroups.close(tab);
                            return;
                        }
                    }
                }
            }
        }
    }
}

export const diffManager = new DiffManager();