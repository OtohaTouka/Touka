import React, { useEffect, useRef, useState } from 'react';
import { useArtifacts } from '@/hooks/useArtifacts';
import { Button } from '@/components/ui/button';
import { 
  Monitor, Code, ChevronLeft, ChevronRight, Rocket, 
  CheckCircle2, AlertCircle, Loader2, Save, X
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { ARTIFACT_SANDBOX, buildWebSrcDoc, buildReactSrcDoc } from '@/lib/artifacts/sandbox';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { bridgeApplyArtifact, isIDE } from '@/lib/bridge';
import DOMPurify from 'dompurify';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import mermaid from 'mermaid';
import { logSSOTError, errorMsg } from '@/lib/error_handler';
import { useTranslation } from 'react-i18next';

// 初始化 Mermaid
mermaid.initialize({
  startOnLoad: false, // 改為 false，由渲染器手動觸發
  theme: 'dark',
  securityLevel: 'strict', // P2 fix: was 'loose' — XSS risk
  fontFamily: 'monospace',
  // @ts-expect-error - Mermaid 某些版本支援此選項隱藏氣泡錯誤
  suppressError: true,
  // 隱藏報錯懸浮氣泡的 CSS 注入
  themeVariables: {
    errorBkgColor: 'transparent',
    errorTextColor: 'transparent'
  }
});

export default function ArtifactView() {
  const { t } = useTranslation();
  const { artifacts, currentArtifactId, setCurrentArtifact } = useArtifacts();
  const currentArtifact = artifacts.find(a => a.id === currentArtifactId);
  
  const [viewMode, setViewMode] = useState<'preview' | 'code'>('preview');
  const [scaleMode, setScaleMode] = useState<'fit' | 'original'>('fit');
  const [status, setStatus] = useState<'idle' | 'loading' | 'success' | 'error'>('idle');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    // 當 Artifact 切換時重置狀態
    setStatus('idle');
    setErrorMessage(null);
  }, [currentArtifactId]);

  if (!currentArtifact) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-muted-foreground p-8 text-center">
        <Monitor className="w-12 h-12 mb-4 opacity-20" />
        <p className="text-sm">{t('artifacts.artifact_view.empty_no_artifact')}</p>
        <p className="text-xs mt-2 opacity-60">{t('artifacts.artifact_view.empty_hint')}</p>
      </div>
    );
  }

  const handleApplyToIDE = async () => {
    setStatus('loading');
    setErrorMessage(null);

    const content = currentArtifact.content;
    const title = currentArtifact.title;

    try {
      const success = await bridgeApplyArtifact({
        content,
        title,
        type: currentArtifact.type,
        proxyUrl: getActiveProxyUrl(),
        fetchTouka
      });

      if (success) {
        setStatus('success');
        if (!isIDE()) {
           setTimeout(() => setStatus('idle'), 2000);
        }
      } else {
        throw new Error(errorMsg("SYNC_FAILED"));
      }
    } catch (err) {
      const error = err as Error;
      logSSOTError(error, "ArtifactView:handleApplyToIDE");
      setStatus('error');
      setErrorMessage(error.message);
    }
  };

  return (
    <div className="flex flex-col h-full bg-card">
      {/* Artifact Header */}
      <div className="p-3 border-b border-border flex items-center justify-between bg-muted/30 z-30 shrink-0">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-primary/10 rounded-lg">
            {currentArtifact.type === 'web' || currentArtifact.type === 'react' ? <Monitor className="w-4 h-4 text-primary" /> :
             currentArtifact.type === 'mermaid' ? <Rocket className="w-4 h-4 text-primary" /> :
             <Code className="w-4 h-4 text-primary" />}
          </div>
          <div className="min-w-0">
            <h4 className="text-sm font-bold truncate w-48" title={currentArtifact.title}>{currentArtifact.title}</h4>
            <p className="text-xs text-muted-foreground uppercase font-black tracking-widest">{currentArtifact.type}</p>
          </div>
        </div>

        <div className="flex items-center gap-1">
          {/* Scale Controls (Only for Preview) */}
          {viewMode === 'preview' && (currentArtifact.type === 'web' || currentArtifact.type === 'react' || currentArtifact.type === 'mermaid') && (
            <div className="flex bg-muted p-0.5 rounded-md self-center mr-1">
              <button
                onClick={() => setScaleMode('fit')}
                className={cn(
                  "px-2 py-0.5 text-xs font-bold rounded-sm transition-all",
                  scaleMode === 'fit' ? "bg-background shadow-sm text-primary" : "text-muted-foreground hover:text-foreground"
                )}
              >
                FIT
              </button>
              <button
                onClick={() => setScaleMode('original')}
                className={cn(
                  "px-2 py-0.5 text-xs font-bold rounded-sm transition-all",
                  scaleMode === 'original' ? "bg-background shadow-sm text-primary" : "text-muted-foreground hover:text-foreground"
                )}
              >
                SIZE
              </button>
            </div>
          )}

          <Button 
            variant={status === 'success' ? "ghost" : "outline"}
            size="sm" 
            className={cn(
              "h-8 px-2 text-xs gap-1.5 transition-all duration-300",
              status === 'success' ? "text-status-success bg-status-success/10 border-status-success/20" : 
              status === 'error' ? "text-status-error border-status-error/20" :
              "border-primary/30 hover:bg-primary/10 text-primary"
            )}
            onClick={handleApplyToIDE}
            disabled={status === 'loading'}
          >
            {status === 'loading' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> :
             status === 'success' ? <CheckCircle2 className="w-3.5 h-3.5" /> :
             status === 'error' ? <AlertCircle className="w-3.5 h-3.5" /> :
             <Save className="w-3.5 h-3.5" />}
            
            {status === 'loading' ? "Applying..." : 
             status === 'success' ? "Applied" : 
             status === 'error' ? "Retry" :
             "Apply to Project"}
          </Button>
        </div>
      </div>

      {/* Error Message Bar */}
      {status === 'error' && (
        <div className="bg-status-error/10 p-2 px-3 border-b border-status-error/20 flex items-center justify-between gap-2 animate-in fade-in slide-in-from-top-1">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-3.5 h-3.5 text-status-error" />
            <p className="text-xs text-status-error font-medium">{errorMessage}</p>
          </div>
          <button 
            onClick={() => { setStatus('idle'); setErrorMessage(null); }}
            className="text-status-error/60 hover:text-status-error transition-colors"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* View Mode Toggle */}
      <div className="flex border-b border-border p-1 gap-1">
        <button 
          onClick={() => setViewMode('preview')}
          className={cn(
            "flex-1 py-1.5 text-xs font-bold rounded-md transition-all",
            viewMode === 'preview' ? "bg-background shadow-sm text-primary" : "text-muted-foreground hover:text-foreground"
          )}
        >
          PREVIEW
        </button>
        <button 
          onClick={() => setViewMode('code')}
          className={cn(
            "flex-1 py-1.5 text-xs font-bold rounded-md transition-all",
            viewMode === 'code' ? "bg-background shadow-sm text-primary" : "text-muted-foreground hover:text-foreground"
          )}
        >
          CODE
        </button>
      </div>

      {/* Content Area */}
      <div className="flex-1 overflow-auto relative">
        {viewMode === 'preview' ? (
          <div className="h-full">
            <ErrorBoundary key={currentArtifact.id} fallback={<div className="p-8 text-center text-xs text-status-error">{t('artifacts.artifact_view.render_fatal_error')}</div>}>
              {currentArtifact.type === 'web' && <WebRenderer content={currentArtifact.content} scaleMode={scaleMode} />}
              {currentArtifact.type === 'react' && <ReactRenderer content={currentArtifact.content} scaleMode={scaleMode} />}
              {currentArtifact.type === 'mermaid' && <MermaidRenderer content={currentArtifact.content} scaleMode={scaleMode} />}
              {currentArtifact.type === 'markdown' && <MarkdownRenderer content={currentArtifact.content} />}
            </ErrorBoundary>
          </div>
        ) : (
          <div className="p-4 font-mono text-xs whitespace-pre bg-muted/50 text-foreground min-h-full overflow-auto selection:bg-primary/30">
            {currentArtifact.content}
          </div>
        )}
      </div>

      {/* Artifact Navigation (if multiple) */}
      {artifacts.length > 1 && (
        <div className="p-2 border-t border-border flex items-center justify-between bg-muted/30 shrink-0">
          <Button 
            variant="ghost" 
            size="sm" 
            className="h-7 text-xs px-2"
            onClick={() => {
              const idx = artifacts.findIndex(a => a.id === currentArtifactId);
              if (idx < artifacts.length - 1) setCurrentArtifact(artifacts[idx + 1].id);
            }}
            disabled={artifacts.findIndex(a => a.id === currentArtifactId) === artifacts.length - 1}
          >
            <ChevronLeft className="w-3 h-3 mr-1" /> Older
          </Button>
          <span className="text-xs text-muted-foreground font-bold">
            {artifacts.findIndex(a => a.id === currentArtifactId) + 1} / {artifacts.length}
          </span>
          <Button 
            variant="ghost" 
            size="sm" 
            className="h-7 text-xs px-2"
            onClick={() => {
              const idx = artifacts.findIndex(a => a.id === currentArtifactId);
              if (idx > 0) setCurrentArtifact(artifacts[idx - 1].id);
            }}
            disabled={artifacts.findIndex(a => a.id === currentArtifactId) === 0}
          >
            Newer <ChevronRight className="w-3 h-3 ml-1" />
          </Button>
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------------------
// Renderers
// ----------------------------------------------------------------------------

function WebRenderer({ content, scaleMode }: { content: string, scaleMode: 'fit' | 'original' }) {
  const iframeRef = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    if (!iframeRef.current) return;
    // CSP / sandbox / doc-shell 皆出自 sandbox.ts 的 SSOT（ADR-0114；沿襲 P12/DA10）：
    // connect-src 'none'（無外連/無外洩）、無外部 script（無 CDN）、sandbox 永無 same-origin。
    iframeRef.current.srcdoc = buildWebSrcDoc(content, scaleMode);
  }, [content, scaleMode]);

  return (
    <div className={cn(
      "w-full h-full overflow-auto custom-scrollbar",
      scaleMode === 'fit' ? "overflow-hidden" : "overflow-auto"
    )}>
      <iframe
        ref={iframeRef}
        title="Web Artifact"
        className="w-full h-full border-0 bg-transparent"
        sandbox={ARTIFACT_SANDBOX}
      />
    </div>
  );
}

/**
 * 互動式 React/JSX artifact 渲染器（選項 A，ADR-0114）。
 *
 * 在父層 lazy 載入 Babel 轉譯器 ＋ 預建 React runtime，轉譯使用者碼為純 JS（**父層絕不執行**），
 * 組成 srcdoc 注入沙箱 iframe（同 WebRenderer 的 CSP/sandbox：離線、無 same-origin）由 React
 * 渲染、可互動。轉譯失敗顯示父層 i18n 錯誤；iframe 內執行錯誤由內嵌 error 顯示器處理。
 */
function ReactRenderer({ content, scaleMode }: { content: string, scaleMode: 'fit' | 'original' }) {
  const { t } = useTranslation();
  const [srcDoc, setSrcDoc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSrcDoc(null);
    // 防抖：串流期間 content 每個 chunk 都變，若每次都轉譯＝主執行緒 O(n²) 凍結（ADR-0114 複審
    // DoS）。延後到 content 穩定 ~300ms（通常＝串流結束）才轉譯；期間僅顯示 spinner，也避免對
    // 未完成的 JSX 反覆報轉譯錯誤。
    const timer = setTimeout(() => {
    void (async () => {
      try {
        // lazy：Babel（~3MB）與 runtime bundle 僅在開啟 React artifact 時載入，不進初始 bundle。
        const [{ transpileArtifact }, { ARTIFACT_RUNTIME }] = await Promise.all([
          import('@/lib/artifacts/transpile'),
          import('@/generated/artifactRuntime'),
        ]);
        const result = await transpileArtifact(content);
        if (cancelled) return;
        if ('error' in result) {
          setError(t('artifacts.react.transpile_failed', { error: result.error }));
          setLoading(false);
          return;
        }
        setSrcDoc(buildReactSrcDoc({
          runtime: ARTIFACT_RUNTIME,
          code: result.code,
          scaleMode,
          messages: {
            noComponent: t('artifacts.react.no_component'),
            noImportPrefix: t('artifacts.react.no_import_prefix'),
            runtimeUnavailable: t('artifacts.react.runtime_unavailable'),
          },
        }));
        setLoading(false);
      } catch (e) {
        if (cancelled) return;
        logSSOTError(e, 'ArtifactView:ReactRenderer');
        setError(t('artifacts.react.transpile_failed', { error: errorMsg('SYSTEM_ERROR') }));
        setLoading(false);
      }
    })();
    }, 300);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [content, scaleMode, t]);

  if (error) {
    return <div className="p-4 text-xs text-destructive font-mono whitespace-pre-wrap break-all">{error}</div>;
  }
  if (loading || !srcDoc) {
    return (
      <div className="h-full flex items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }

  return (
    <div className={cn(
      "w-full h-full",
      scaleMode === 'fit' ? "overflow-hidden" : "overflow-auto custom-scrollbar"
    )}>
      <iframe
        title="React Artifact"
        className="w-full h-full border-0 bg-transparent"
        sandbox={ARTIFACT_SANDBOX}
        srcDoc={srcDoc}
      />
    </div>
  );
}

function MermaidRenderer({ content, scaleMode }: { content: string, scaleMode: 'fit' | 'original' }) {
  const { t } = useTranslation();
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const lastSuccessSvg = useRef<string>('');
  const [uniqueId] = useState(() => `mermaid-${Math.random().toString(36).substring(7)}`);

  useEffect(() => {
    const render = async () => {
      try {
        if (content.length < 10) return;
        
        // 渲染前清理與重置
        const { svg: renderedSvg } = await mermaid.render(uniqueId, content);
        setSvg(renderedSvg);
        lastSuccessSvg.current = renderedSvg;
        setError(null);
      } catch (err) {
        logSSOTError(err, "ArtifactView:MermaidRenderer");
        // 如果渲染失敗且之前有成功過，繼續顯示舊的圖
        if (lastSuccessSvg.current) {
          setSvg(lastSuccessSvg.current);
        } else {
          setError(t('artifacts.artifact_view.mermaid_awaiting_valid_syntax'));
        }
        
        // 清理 Mermaid 產生的殘留元素 (Mermaid 10+ 渲染失敗時常留下的 div)
        const leftover = document.getElementById(`d${uniqueId}`);
        if (leftover) leftover.remove();
      }
    };
    render();
  }, [content, uniqueId]);

  if (error && !svg) return <div className="p-4 text-xs text-muted-foreground font-mono animate-pulse">{error}</div>;
  
  return (
    <div className={cn(
      "p-4 flex min-h-full bg-muted/5 transition-opacity duration-300 custom-scrollbar",
      scaleMode === 'fit' ? "items-center justify-center overflow-hidden" : "items-start justify-start overflow-auto"
    )}>
      <div 
        className={cn(
          "transition-all duration-300",
          scaleMode === 'fit' ? "w-full max-w-full overflow-hidden" : "w-max"
        )}
        style={scaleMode === 'fit' ? { maxWidth: '100%' } : {}}
        dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } }) }}
      />
    </div>
  );
}

function MarkdownRenderer({ content }: { content: string }) {
  return (
    <div className="p-6 prose dark:prose-invert prose-sm max-w-none">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {content}
      </ReactMarkdown>
    </div>
  );
}

// ----------------------------------------------------------------------------
// Error Boundary
// ----------------------------------------------------------------------------

class ErrorBoundary extends React.Component<{ children: React.ReactNode, fallback: React.ReactNode }, { hasError: boolean }> {
  constructor(props: { children: React.ReactNode, fallback: React.ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }
  
  componentDidCatch(error: Error) {
    logSSOTError(error, "ArtifactView:ErrorBoundary");
  }
  
  render() {
    if (this.state.hasError) {
      return this.props.fallback;
    }
    return this.props.children;
  }
}
