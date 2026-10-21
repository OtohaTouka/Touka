import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

export class DiffManager {
    constructor() {}

    /**
     * Proposes changes to a file by opening a native VS Code diff view.
     * @param originalPath The absolute path of the file to modify.
     * @param newContent The new content to write.
     * @returns A promise that resolves with a success or rejection message.
     */
    public async proposeChanges(originalPath: string, newContent: string): Promise<string> {
        return new Promise(async (resolve) => {
            try {
                const originalUri = vscode.Uri.file(originalPath);
                const fileExists = fs.existsSync(originalPath);
                
                // Read original content if it exists, otherwise empty
                const originalContent = fileExists ? await fs.promises.readFile(originalPath, 'utf8') : '';

                // If content is exactly the same, no need to diff
                if (originalContent === newContent) {
                    return resolve("No changes detected. The new content is identical to the existing file.");
                }

                // Create a temporary file for the new content
                const tempDir = os.tmpdir();
                const fileName = path.basename(originalPath);
                const ext = path.extname(originalPath);
                const baseNameWithoutExt = path.basename(originalPath, ext);
                const tempFileName = `${baseNameWithoutExt}.touka-diff-${Date.now()}${ext}`;
                const tempFilePath = path.join(tempDir, tempFileName);
                
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
                    if (selection === "Accept (Apply)") {
                        try {
                            // Ensure directory exists for new files
                            const dir = path.dirname(originalPath);
                            if (!fs.existsSync(dir)) {
                                await fs.promises.mkdir(dir, { recursive: true });
                            }
                            // Write the new content to the actual file
                            await fs.promises.writeFile(originalPath, newContent, 'utf8');
                            resolve(`Success: User accepted and applied the changes to ${fileName}.`);
                        } catch (err: any) {
                            resolve(`Error applying changes: ${err.message}`);
                        } finally {
                            this.cleanupTempFile(tempFilePath);
                            this.closeDiffEditor(tempUri);
                        }
                    } else {
                        // User rejected or dismissed the notification
                        this.cleanupTempFile(tempFilePath);
                        this.closeDiffEditor(tempUri);
                        resolve(`Rejected: User denied the proposed changes to ${fileName}.`);
                    }
                });

            } catch (error: any) {
                resolve(`Error initiating diff: ${error.message}`);
            }
        });
    }

    private async cleanupTempFile(tempPath: string) {
        try {
            if (fs.existsSync(tempPath)) {
                await fs.promises.unlink(tempPath);
            }
        } catch (e) {
            console.error(`[ToukaIDE] Failed to cleanup temp file: ${tempPath}`, e);
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