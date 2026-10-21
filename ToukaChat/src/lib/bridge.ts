/**
 * ToukaBridge - 統一的 IDE 與 Web 通訊適配器
 * 
 * 這裡擔任了系統的 Adapter 層，將 VS Code 專屬的 postMessage 
 * 與 Web 端的 fetch 或 Event 通訊邏輯隔離。
 */
import { logSSOTError } from "./error_handler";

export interface BridgeMessage {
  command?: string;
  type?: string;
  [key: string]: unknown;
}

/** VS Code webview 注入的最小 API 介面 */
interface VsCodeApi {
  postMessage: (message: unknown) => void;
}

/**
 * 判斷目前是否運行於 VS Code 擴充功能環境中
 */
export const isIDE = (): boolean => {
  if (typeof window === 'undefined') return false;
  return !!(window as unknown as { vscode?: VsCodeApi }).vscode;
};

/**
 * 向環境（IDE 或 Web）發送訊息
 */
export const postToBridge = (message: BridgeMessage) => {
  if (isIDE()) {
    const vscode = (window as unknown as { vscode?: VsCodeApi }).vscode;
    if (vscode && typeof vscode.postMessage === 'function') {
      vscode.postMessage(message);
    }
  } else {
    // Web 模式下的自定義事件處理（若有需要讓其他組件監聽）
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('touka_bridge_message', { detail: message }));
    }
  }
};

/**
 * 專用的 Apply Artifact 適配器
 * 會根據環境決定是發送給 VS Code 還是透過 Proxy 寫入檔案
 */
export const bridgeApplyArtifact = async (params: {
  content: string;
  title: string;
  type: string;
  language?: string;
  proxyUrl: string;
  fetchTouka: (proxyUrl: string, path: string, options?: RequestInit) => Promise<Response>;
}) => {
  if (isIDE()) {
    postToBridge({
      command: 'touka.applyArtifact',
      content: params.content,
      title: params.title,
      type: params.type
    });
    return true;
  } else {
    const filename = params.title.includes('.') 
      ? params.title 
      : `${params.title.replace(/\s+/g, '_').toLowerCase()}.${params.language || 'txt'}`;
    
    try {
      const response = await params.fetchTouka(params.proxyUrl, "/editor/write", {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: filename,
          content: params.content
        })
      });
      return response.ok;
    } catch (e) {
      logSSOTError(e, "Bridge:bridgeApplyArtifact");
      return false;
    }
  }
};
