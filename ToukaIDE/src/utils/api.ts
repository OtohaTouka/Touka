import * as vscode from 'vscode';

/**
 * 從 VSCode 配置中獲取 Proxy URL
 */
export function getProxyUrl(): string {
    const config = vscode.workspace.getConfiguration('touka');
    const url = config.get<string>('proxyUrl');
    
    if (!url) {
        const errorMsg = "Touka Proxy URL is not configured. Please set 'touka.proxyUrl' in settings.";
        vscode.window.showErrorMessage(`❌ [ToukaIDE] ${errorMsg}`);
        return ""; // 返回空字串讓後續 fetch 失敗
    }
    
    return url.replace(/\/$/, "");
}

/**
 * 從 VSCode 配置中獲取 API Key
 */
export function getApiKey(): string {
    const config = vscode.workspace.getConfiguration('touka');
    return config.get<string>('apiKey') || "local-dev";
}

/**
 * Common fetch wrapper for Touka Proxy
 */
export async function fetchTouka(path: string, options: RequestInit = {}): Promise<Response> {
    const proxyUrl = getProxyUrl();
    if (!proxyUrl) {
        throw new Error("Proxy URL not configured");
    }

    const apiKey = getApiKey();
    const url = `${proxyUrl}${path.startsWith('/') ? path : `/${path}`}`;
    const defaultHeaders = {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`
    };


    return fetch(url, {
        ...options,
        headers: {
            ...defaultHeaders,
            ...options.headers
        }
    });
}
