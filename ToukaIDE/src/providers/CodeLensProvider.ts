import * as vscode from 'vscode';

export class ToukaCodeLensProvider implements vscode.CodeLensProvider {
    // Regex for basic function/class detection in TS/JS/Python
    private regex: RegExp;

    constructor() {
        this.regex = /(?:function\s+([a-zA-Z0-9_]+))|(?:class\s+([a-zA-Z0-9_]+))|(?:(?:const|let|var)\s+([a-zA-Z0-9_]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>)|(?:def\s+([a-zA-Z0-9_]+)\s*\()/g;
    }

    public provideCodeLenses(document: vscode.TextDocument, token: vscode.CancellationToken): vscode.CodeLens[] | Thenable<vscode.CodeLens[]> {
        const codeLenses: vscode.CodeLens[] = [];
        const text = document.getText();
        
        // Reset lastIndex because of /g flag in the regex property
        this.regex.lastIndex = 0;
        let matches;

        while ((matches = this.regex.exec(text)) !== null) {
            const line = document.positionAt(matches.index).line;
            const indexOf = matches[0].indexOf(matches[1] || matches[2] || matches[3] || matches[4]);
            const position = document.positionAt(matches.index + indexOf);
            const range = document.getWordRangeAtPosition(position, new RegExp('[a-zA-Z0-9_]+'));

            if (range) {
                // Name of the function/class
                const symbolName = matches[1] || matches[2] || matches[3] || matches[4];

                // 1. Explain CodeLens
                codeLenses.push(new vscode.CodeLens(range, {
                    title: "✨ Touka: 解釋",
                    tooltip: "請 Touka 解釋這段代碼",
                    command: "toukaide.action.explain",
                    arguments: [document.uri, range, symbolName]
                }));

                // 2. Refactor CodeLens
                codeLenses.push(new vscode.CodeLens(range, {
                    title: "🛠️ Touka: 重構",
                    tooltip: "請 Touka 幫忙重構這段代碼",
                    command: "toukaide.action.refactor",
                    arguments: [document.uri, range, symbolName]
                }));
            }
        }
        return codeLenses;
    }
}