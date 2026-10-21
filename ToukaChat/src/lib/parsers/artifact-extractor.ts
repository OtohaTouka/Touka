import { ArtifactType } from '@/hooks/useArtifacts';

export interface ExtractedArtifact {
  title: string;
  type: ArtifactType;
  content: string;
  language?: string;
}

const VALID_TYPES: ReadonlySet<string> = new Set(['web', 'react', 'mermaid', 'markdown', 'code']);

/**
 * 正規化並驗證模型自填的 `<touka_artifact type="...">`（ADR-0114 複審）：大小寫不敏感、不在
 * 白名單即安全降級為 'code'（純顯示，不執行）；顯式 type='react' 但內容是完整 HTML 文件則
 * 降級為 'web'（不送進轉譯），與 fenced-block 的 isHtmlDoc 規則一致。
 */
function normalizeTagType(raw: string, content: string): ArtifactType {
  const t = (raw || '').toLowerCase().trim();
  const valid: ArtifactType = VALID_TYPES.has(t) ? (t as ArtifactType) : 'code';
  if (valid === 'react') {
    const lower = content.toLowerCase();
    if (lower.includes('<html') || lower.includes('<!doctype')) return 'web';
  }
  return valid;
}

/**
 * 優化版的 Artifact 提取器
 */
export function extractArtifacts(text: string): ExtractedArtifact[] {
  const artifacts: ExtractedArtifact[] = [];
  const lines = text.split('\n');
  
  // 輔助函式：從代碼塊上方尋找潛在標題
  const findTitleAbove = (index: number, defaultTitle: string): string => {
    // 向上搜尋最多 3 行，尋找粗體或包含關鍵字的句子
    for (let i = index - 1; i >= Math.max(0, index - 3); i--) {
      const line = lines[i].trim();
      if (!line) continue;
      
      // 匹配 **標題** 或 ### 標題 或 這是...的程式碼
      const boldMatch = line.match(/\*\*(.*?)\*\*/);
      if (boldMatch) return boldMatch[1];
      
      const headerMatch = line.match(/#+\s+(.*)/);
      if (headerMatch) return headerMatch[1];
      
      if (line.includes('這是') || line.includes('Here is')) {
        return line.replace(/[臉這是：:。]/g, '').trim();
      }
      
      // 如果是一句短語，也可能是標題
      if (line.length < 30 && line.length > 2) return line;
    }
    return defaultTitle;
  };

  // 1. 處理明確的 <touka_artifact> 標籤 (最高優先級)
  const tagRegex = /<touka_artifact\s+title="([^"]+)"\s+type="([^"]+)"(?:\s+language="([^"]+)")?>\n([\s\S]*?)<\/touka_artifact>/g;
  let tagMatch;
  while ((tagMatch = tagRegex.exec(text)) !== null) {
    const tagContent = tagMatch[4].trim();
    artifacts.push({
      title: tagMatch[1],
      type: normalizeTagType(tagMatch[2], tagContent),
      language: tagMatch[3],
      content: tagContent,
    });
  }

  // 2. 掃描 Markdown 代碼塊 (支援未閉合區塊用於 Streaming)
  const codeBlockRegex = /```(\w+)?\n([\s\S]*?)(?:```|$)/g;
  let match;
  while ((match = codeBlockRegex.exec(text)) !== null) {
    const lang = (match[1] || '').toLowerCase();
    const content = match[2].trim();
    // 用 RegExp 迭代器的實際位移（match.index），而非 indexOf——後者命中「第一個」相同字串，
    // 會把重複代碼塊的標題歸錯（ADR-0114 複審）。
    const startIndex = match.index;
    const lineIndex = text.substring(0, startIndex).split('\n').length - 1;

    // --- Mermaid 圖表 ---
    const mermaidTags = ['mermaid', 'mmd', 'graph', 'flowchart', 'sequencediagram', 'statediagram', 'erdiagram', 'classdiagram', 'gantt', 'pie'];
    const isMermaid = mermaidTags.includes(lang) || 
                      content.startsWith('graph ') || 
                      content.startsWith('graph\n') ||
                      content.startsWith('flowchart ') ||
                      content.startsWith('sequenceDiagram') ||
                      content.startsWith('stateDiagram') ||
                      content.startsWith('erDiagram');

    if (isMermaid) {
      artifacts.push({
        title: findTitleAbove(lineIndex, "流程架構圖"),
        type: 'mermaid',
        content: content,
      });
      continue;
    }

    // --- Web / UI 預覽 (HTML, TSX, JSX, Tailwind) ---
    const isUI = lang === 'html' || lang === 'tsx' || lang === 'jsx' || lang === 'vue';
    const hasUIMarkers = content.includes('<html') ||
                         content.includes('import React') ||
                         (content.includes('<div') && content.includes('className='));

    if (isUI || hasUIMarkers) {
      // 過濾掉純邏輯代碼，只提取有 UI 結構的
      if (content.includes('<') && content.includes('>')) {
        // 區分「互動式 React 元件」(走 ReactRenderer，會轉譯並真的跑) 與「純 HTML 片段」
        // (走 WebRenderer，原樣渲染)。ADR-0114：React 訊號＝tsx/jsx、import React、export
        // default、hooks 呼叫、className=、或大寫開頭的 function/const 元件；但完整 HTML 文件
        // (lang=html 或含 <html>/<!doctype>) 一律當純 HTML。
        const lower = content.toLowerCase();
        const isHtmlDoc = lang === 'html' || lower.includes('<html') || lower.includes('<!doctype');
        const reactSignals =
          lang === 'tsx' || lang === 'jsx' ||
          /\bimport\s+React\b/.test(content) ||
          /\bexport\s+default\b/.test(content) ||
          /\buse[A-Z]\w+\s*\(/.test(content) ||
          /className=/.test(content) ||
          /\b(?:function|const)\s+[A-Z]\w*/.test(content);
        const type: ArtifactType = !isHtmlDoc && reactSignals ? 'react' : 'web';
        artifacts.push({
          title: findTitleAbove(lineIndex, "UI 組件預覽"),
          type,
          content: content,
        });
      }
      continue;
    }

    // --- SVG 直接渲染 ---
    if (lang === 'svg' || (content.startsWith('<svg') && content.endsWith('</svg>'))) {
      artifacts.push({
        title: findTitleAbove(lineIndex, "向量圖形"),
        type: 'web', // SVG 也可以透過 WebRenderer 渲染
        content: content,
      });
      continue;
    }

    // --- 長篇 Markdown 文檔 ---
    if (lang === 'markdown' || lang === 'md') {
      artifacts.push({
        title: findTitleAbove(lineIndex, "專案文檔"),
        type: 'markdown',
        content: content,
      });
      continue;
    }
  }

  // 3. 去重：如果內容完全相同則只保留一個
  const uniqueArtifacts = artifacts.filter((v, i, a) => 
    a.findIndex(t => t.content === v.content) === i
  );

  return uniqueArtifacts;
}
