import * as vscode from 'vscode';

export function handleGetDiagnostics(uriStr?: string): string {
    let diagnostics: [vscode.Uri, vscode.Diagnostic[]][];
    if (uriStr) {
        const uri = vscode.Uri.parse(uriStr);
        diagnostics = [[uri, vscode.languages.getDiagnostics(uri)]];
    } else {
        diagnostics = vscode.languages.getDiagnostics();
    }

    let result = "";
    for (const [uri, diags] of diagnostics) {
        if (diags.length === 0) continue;
        result += `File: ${uri.fsPath}\n`;
        for (const d of diags) {
            const severity = vscode.DiagnosticSeverity[d.severity];
            result += `  [${severity}] Line ${d.range.start.line + 1}: ${d.message}\n`;
        }
    }
    return result || "No diagnostics found.";
}

export function handleGetOpenFile(): string {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return "No active text editor.";
    
    const cursorLine = editor.selection.active.line + 1;
    const visibleRanges = editor.visibleRanges;
    let visibleInfo = "No visible range info";
    if (visibleRanges.length > 0) {
        const start = visibleRanges[0].start.line + 1;
        const end = visibleRanges[0].end.line + 1;
        visibleInfo = `Lines ${start} - ${end}`;
    }

    return `File: ${editor.document.fileName}\nCursor Line: ${cursorLine}\nVisible Range: ${visibleInfo}\n\n${editor.document.getText()}`;
}

export function handleGetSelection(): string {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return "No active text editor.";
    const selection = editor.selection;
    if (selection.isEmpty) return "No text selected.";
    
    let selectedText = editor.document.getText(selection);
    const MAX_LENGTH = 16384; // 16KB limit
    
    if (selectedText.length > MAX_LENGTH) {
        selectedText = selectedText.substring(0, MAX_LENGTH) + "\n\n... [Selection truncated to 16KB limit]";
    }
    
    return selectedText;
}
