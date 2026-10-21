import * as vscode from 'vscode';
import { fetchTouka } from '../utils/api';

export async function handleInlineChat() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showInformationMessage("請先開啟一個文字編輯器。");
        return;
    }

    const selection = editor.selection;
    if (selection.isEmpty) {
        vscode.window.showInformationMessage("請先選取要修改的代碼。");
        return;
    }

    const instruction = await vscode.window.showInputBox({
        prompt: "✨ Touka 行內編輯：請輸入修改指令",
        placeHolder: "例如：加上註解、重構成 async/await"
    });

    if (!instruction) return;

    const selectedText = editor.document.getText(selection);
    vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: "Touka 正在思考修改...",
        cancellable: true
    }, async (progress, token) => {
        try {
            const response = await fetchTouka('/v1/chat/completions', {
                method: 'POST',
                body: JSON.stringify({
                    model: 'default',
                    messages: [
                        { role: 'system', content: "You are an inline code editor. Reply ONLY with the modified code that replaces the user's selection. Do not include markdown formatting like ``` if it replaces code directly." },
                        { role: 'user', content: `指令: ${instruction}\n\n代碼:\n${selectedText}` }
                    ]
                })
            });

            if (response.ok) {
                const data = await response.json() as { choices?: { message?: { content?: string } }[] };
                let newText = data.choices?.[0]?.message?.content || '';
                if (newText.startsWith('```')) {
                    newText = newText.replace(/```[a-z]*\n/g, '').replace(/```/g, '');
                }
                
                editor.edit(editBuilder => {
                    editBuilder.replace(selection, newText);
                });
            }
        } catch (err) {
            vscode.window.showErrorMessage("行內編輯失敗，請檢查 Proxy 連線。");
        }
    });
}
