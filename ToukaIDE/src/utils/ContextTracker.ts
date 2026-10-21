import * as vscode from 'vscode';

export class ContextTracker {
    private recentFiles: string[] = [];
    private readonly MAX_RECENT_FILES = 10;
    private disposable: vscode.Disposable | null = null;

    constructor() {}

    public track() {
        // Listen to active text editor changes
        this.disposable = vscode.window.onDidChangeActiveTextEditor(editor => {
            if (editor && editor.document.uri.scheme === 'file') {
                this.addFile(editor.document.uri.fsPath);
            }
        });

        // Initialize with the currently active editor if one exists
        if (vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri.scheme === 'file') {
            this.addFile(vscode.window.activeTextEditor.document.uri.fsPath);
        }
    }

    private addFile(fsPath: string) {
        // Remove if it already exists to put it at the front
        this.recentFiles = this.recentFiles.filter(p => p !== fsPath);
        
        // Add to the front
        this.recentFiles.unshift(fsPath);
        
        // Trim to max length
        if (this.recentFiles.length > this.MAX_RECENT_FILES) {
            this.recentFiles = this.recentFiles.slice(0, this.MAX_RECENT_FILES);
        }
    }

    public getRecentFiles(): string[] {
        return this.recentFiles;
    }

    public dispose() {
        if (this.disposable) {
            this.disposable.dispose();
        }
    }
}

// Export a singleton instance
export const contextTracker = new ContextTracker();
