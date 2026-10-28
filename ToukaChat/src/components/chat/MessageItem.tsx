"use client";

import React from 'react';
import { useTranslation } from "react-i18next";
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import Image from 'next/image';
import { cn } from "@/lib/utils";
import { Button } from '@/components/ui/button';
import {
  Copy, Play, Check, ChevronLeft, ChevronRight, Edit, Delete, RefreshCcw, Loader2, ThumbsUp, ThumbsDown, FileText
} from 'lucide-react';
import { formatBytes } from '@/lib/chat/uploadClassify';
import { ToolStepTracker } from "./ToolStepTracker";
import { InterjectionMarker } from "./InterjectionMarker";
import { AgentActivityStrip } from "./AgentActivityStrip";
import { ThinkingFold } from "./ThinkingFold";
import { MessageNode, TreeHistory } from '@/lib/tree_history';
import { useTheme } from "@/lib/ThemeContext";

interface MessageItemProps {
  msg: MessageNode;
  history: TreeHistory;
  isSending: boolean;
  isIDE: boolean;
  editingMessageId: string | null;
  setEditingMessageId: (id: string | null) => void;
  editValue: string;
  setEditValue: (val: string) => void;
  handleCopy: (text: string) => void;
  handleEditMessage: (id: string, text: string) => void;
  handleDeleteMessage: (id: string) => void;
  handleRegenerate: (id: string) => void;
  handleSwitchMessage: (id: string) => void;
  handleApplyCode: (code: string, lang?: string) => Promise<boolean>;
  /** HAX G15：此訊息目前的回饋狀態（讚/倒讚）。 */
  feedback?: { type: string; reason?: string | null } | null;
  /** HAX G15：送出/切換/撤回回饋（type='none' 為撤回）。 */
  onFeedback?: (id: string, type: 'up' | 'down' | 'none', reason?: string) => void;
  /** HAX G15：倒讚時請求填寫原因（開啟原因彈窗）。 */
  onRequestDislike?: (id: string) => void;
}

export const MessageItem = React.memo(({
  msg, history, isSending, isIDE,
  editingMessageId, setEditingMessageId, editValue, setEditValue,
  handleCopy, handleEditMessage, handleDeleteMessage,
  handleRegenerate, handleSwitchMessage, handleApplyCode,
  feedback, onFeedback, onRequestDislike
}: MessageItemProps) => {
  const { t } = useTranslation();
  // 外觀打磨包：密度（聊天間距）與泡泡樣式（圓角/方角）。IDE 模式維持自身緊湊樣式不受密度影響。
  const { density, bubbleStyle } = useTheme();
  const compact = !isIDE && density === 'compact';
  const square = bubbleStyle === 'square';
  const parent = msg.parentId ? history.messages[msg.parentId] : null;

  const normalizeAgentId = React.useCallback((name: string): string => {
    const lower = name.toLowerCase();
    const mapping: Record<string, string> = {
      '愛麗絲': 'alice',
      '小愛': 'alice',
      '薔薇': 'rose',
      '琥珀': 'amber',
      '除錯': 'debugagent',
      '偵錯': 'debugagent'
    };
    return mapping[lower] || lower;
  }, []);

  const markdownComponents = React.useMemo(() => ({
    pre({ children }: { children?: React.ReactNode }) { return <>{children}</>; },
    code({ className, children, ...props }: React.ComponentPropsWithoutRef<'code'>) {
      const match = /language-(\w+)/.exec(className || '');
      const isBlock = Boolean(className) || String(children).includes('\n');
      const codeContent = String(children).replace(/\n$/, '');
      
      if (!isBlock) {
        return <code className={cn("bg-muted px-1 rounded text-primary font-bold", className)} {...props}>{children}</code>;
      }

      return (
        <div className="group/code relative my-4 rounded-xl overflow-hidden border border-border bg-muted/20 dark:bg-bubble-bg shadow-sm">
          <div className="flex items-center justify-between px-4 py-1.5 bg-muted border-b border-border">
            <span className="text-xs font-black uppercase tracking-widest text-muted-foreground">{match ? match[1] : 'code'}</span>
            <div className="flex items-center gap-2">
              {/* WCAG 2.5.8（A4）：點擊區 ≥24px（p-1.5×2 + icon12 = 24） */}
              <button
                onClick={() => handleCopy(codeContent)}
                className="p-1.5 hover:text-primary transition-colors text-muted-foreground"
                title={t('chat.message_item.copy_code')}
              >
                <Copy size={12} />
              </button>
              <ApplyButton code={codeContent} language={match?.[1]} onApply={handleApplyCode} />
            </div>
          </div>
          <pre className="p-4 overflow-x-auto custom-scrollbar !bg-transparent !m-0 font-mono text-sm font-bold leading-relaxed">
            <code className={cn(className, "!bg-transparent")} {...props}>
              {children}
            </code>
          </pre>
        </div>
      );
    }
  }), [handleCopy, handleApplyCode, t]);
  const siblings = parent 
    ? parent.childrenIds 
    : Object.keys(history.messages).filter(id => !history.messages[id].parentId && history.messages[id].role === 'user');
  const currentBranchIdx = siblings.indexOf(msg.id);

  // 安全刪除判斷：葉節點（最後一則）或有平行分支（可剪取）才允許刪除
  const isLeaf = msg.childrenIds.length === 0;
  const hasSiblings = siblings.length > 1;
  const canDelete = isLeaf || hasSiblings;

  return (
    <div className={cn(
      "flex flex-col gap-2 group/msg w-full min-w-0",
      msg.role === "user" ? "items-end" : "items-start",
      isIDE ? "gap-1" : compact ? "gap-3" : "gap-6"
    )}>
      <div className={cn("flex flex-col gap-1 w-full min-w-0", msg.role === "user" ? "items-end" : "items-start")}>
        <div
          className={cn(
            "leading-relaxed shadow-sm min-w-0 break-words",
            square ? "rounded-md" : "rounded-2xl",
            msg.role === "user"
              ? cn("bg-primary-button text-white", !square && "rounded-tr-none")
              : cn("bg-muted/30 dark:bg-bubble-bg border border-border", !square && "rounded-tl-none"),
            isIDE ? "max-w-[92%] px-3 py-2" : compact ? "max-w-[80%] px-3 py-2" : "max-w-[80%] px-4 py-3"
          )}
          style={{ fontSize: 'var(--chat-font-size)', overflowWrap: 'anywhere' }}
        >
          {msg.attachment && msg.attachment.type === "image" && (
            <div className="mb-3 max-w-sm rounded-xl overflow-hidden border border-border shadow-sm bg-card">
              <Image
                src={msg.attachment.url}
                alt="Uploaded"
                width={800}
                height={600}
                className="w-full h-auto"
                unoptimized
              />
            </div>
          )}
          {msg.attachment && msg.attachment.type !== "image" && msg.attachment.name && (
            <div className="mb-3 inline-flex items-center gap-2 max-w-xs rounded-lg border border-border bg-card/60 px-3 py-2">
              <FileText size={16} className="text-muted-foreground shrink-0" />
              <div className="min-w-0">
                <div className="text-xs font-medium truncate">{msg.attachment.name}</div>
                {typeof msg.attachment.size === "number" && (
                  <div className="text-xs text-muted-foreground">{formatBytes(msg.attachment.size)}</div>
                )}
              </div>
            </div>
          )}
          
          {editingMessageId === msg.id ? (
            <div className="flex flex-col gap-3 w-full">
              <textarea 
                value={editValue} 
                onChange={e => setEditValue(e.target.value)}
                className="w-full bg-muted border border-input rounded-xl p-4 text-foreground focus:ring-2 focus:ring-primary focus:outline-none h-40 resize-none"
                style={{ fontSize: 'var(--chat-font-size)' }}
                autoFocus
              />
              <div className="flex justify-end gap-3">
                <Button variant="ghost" size="sm" onClick={() => setEditingMessageId(null)} className="font-bold">{t('chat.message_item.cancel')}</Button>
                <Button size="sm" onClick={() => handleEditMessage(msg.id, editValue)} className="bg-card text-primary hover:bg-accent font-bold px-6">
                  {msg.role === 'user' ? t('chat.message_item.save_and_resend') : t('chat.message_item.save_edit')}
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {/* ADR-0152 思考摺疊：思考在正文之前（時序＝模型實際產出順序）、預設收合；
                  串流中正文未開始 → 「思考中…」動態標題。 */}
              {msg.role === 'assistant' && msg.thinking && (
                <ThinkingFold thinking={msg.thinking} isThinking={isSending && !msg.content} />
              )}
              {msg.role === 'assistant' && (msg.content || "").includes("**【") ? (
                <div className="space-y-4">
                  {(msg.content || "").split(/\n---\n/).map((part, pi) => {
                    const modelMatch = part.match(/\*\*【(.*?)】\*\*：/);
                    const modelName = modelMatch ? modelMatch[1] : null;
                    const cleanContent = modelName ? part.replace(/\*\*【.*?】\*\*：/, '').trim() : part.trim();
                    
                    if (!cleanContent) return null;

                    const normalizedId = modelName ? normalizeAgentId(modelName) : "";

                    return (
                      <div key={pi} className={cn(
                        "p-4 rounded-xl border border-border/50 bg-bubble-bg relative group/bubble transition-all hover:border-primary/30 shadow-sm",
                        modelName && "pt-8"
                      )}>
                        {modelName && (
                          <div 
                            className="absolute top-2 left-4 text-xs font-black uppercase tracking-widest px-2 py-0.5 rounded-full border"
                            style={{ 
                              color: `var(--color-agent-${normalizedId}, var(--primary))`,
                              backgroundColor: `var(--color-agent-${normalizedId}-accent, color-mix(in srgb, var(--primary) 10%, transparent))`,
                              borderColor: `var(--color-agent-${normalizedId}, var(--primary))`,
                              opacity: 0.8
                            }}
                          >
                            {modelName}
                          </div>
                        )}
                        <div className="prose dark:prose-invert max-w-none break-all leading-relaxed whitespace-pre-wrap" style={{ fontSize: 'inherit' }}>
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                            {cleanContent}
                          </ReactMarkdown>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="prose dark:prose-invert max-w-none break-all leading-relaxed whitespace-pre-wrap" style={{ fontSize: 'inherit' }}>
                  <ReactMarkdown 
                    remarkPlugins={[remarkGfm]}
                    components={markdownComponents}
                  >
                    {msg.content}
                  </ReactMarkdown>
                </div>
              )}
              {msg.role === 'assistant' && msg.progress && (
                <AgentActivityStrip progress={msg.progress} />
              )}
              {msg.role === 'assistant' && msg.steps && msg.steps.length > 0 && (
                <ToolStepTracker steps={msg.steps} />
              )}
              {msg.role === 'assistant' && (msg.interjections?.length || msg.interruptions?.length) ? (
                <InterjectionMarker interjections={msg.interjections ?? []} interruptions={msg.interruptions} />
              ) : null}
              {msg.role === 'user' && msg.hookBlocked ? (
                <p className="text-xs text-muted-foreground">{t('chat.message_item.hook_blocked')}</p>
              ) : null}
            </div>
          )}
        </div>

        <div className={cn(
          "flex items-center gap-1 mt-1 opacity-0 group-hover/msg:opacity-100 transition-opacity", 
          msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'
        )}>
          <span className="text-xs font-bold text-muted-foreground uppercase tracking-widest px-1">
            {msg.role === "user" ? "You" : "Touka AI"}
          </span>
          
          {/* WCAG 2.5.8（A4）：訊息操作鈕點擊區 ≥24px（p-1.5×2 + icon12 = 24） */}
          {!isSending && editingMessageId !== msg.id && (
            <>
              <button onClick={() => handleCopy(msg.content)} className="p-1.5 text-muted-foreground hover:text-primary rounded-md" title={t('chat.message_item.copy')}>
                <Copy size={12} />
              </button>
              <button onClick={() => { setEditingMessageId(msg.id); setEditValue(msg.content); }} className="p-1.5 text-muted-foreground hover:text-primary rounded-md" title={t('chat.message_item.edit')}>
                <Edit size={12} />
              </button>
              {msg.role === 'assistant' && (
                <button onClick={() => handleRegenerate(msg.id)} className="p-1.5 text-muted-foreground hover:text-primary rounded-md" title={t('chat.message_item.regenerate')}>
                  <RefreshCcw size={12} />
                </button>
              )}
              {/* HAX G15：細粒度回饋（讚/倒讚＋倒讚原因）。再點同鍵＝撤回。 */}
              {msg.role === 'assistant' && onFeedback && (
                <>
                  <button
                    onClick={() => onFeedback(msg.id, feedback?.type === 'up' ? 'none' : 'up')}
                    aria-label={t('chat.message_item.thumbs_up')}
                    aria-pressed={feedback?.type === 'up'}
                    title={t('chat.message_item.helpful')}
                    className={cn(
                      "p-1.5 rounded-md transition-colors",
                      feedback?.type === 'up' ? "text-primary" : "text-muted-foreground hover:text-primary"
                    )}
                  >
                    <ThumbsUp size={12} />
                  </button>
                  <button
                    onClick={() => feedback?.type === 'down' ? onFeedback(msg.id, 'none') : onRequestDislike?.(msg.id)}
                    aria-label={t('chat.message_item.thumbs_down')}
                    aria-pressed={feedback?.type === 'down'}
                    title={t('chat.message_item.needs_improvement')}
                    className={cn(
                      "p-1.5 rounded-md transition-colors",
                      feedback?.type === 'down' ? "text-status-error" : "text-muted-foreground hover:text-status-error"
                    )}
                  >
                    <ThumbsDown size={12} />
                  </button>
                </>
              )}
              {canDelete && (
                <button onClick={() => handleDeleteMessage(msg.id)} className="p-1.5 text-muted-foreground hover:text-status-error rounded-md" title={t('chat.message_item.delete_message_and_subsequent')}>
                  <Delete size={12} />
                </button>
              )}
            </>
          )}

          {siblings.length > 1 && (
            <div className="flex items-center gap-1 bg-muted/50 rounded-full px-1.5 py-0.5 border border-border ml-1">
              <button
                disabled={currentBranchIdx <= 0}
                onClick={() => handleSwitchMessage(siblings[currentBranchIdx - 1])}
                className="p-1.5 text-xs disabled:opacity-20 hover:text-primary"
                title={t('chat.message_item.previous_branch')}
              >
                <ChevronLeft size={12} />
              </button>
              <span className="text-xs font-black text-muted-foreground tabular-nums">
                {currentBranchIdx + 1}/{siblings.length}
              </span>
              <button
                disabled={currentBranchIdx >= siblings.length - 1}
                onClick={() => handleSwitchMessage(siblings[currentBranchIdx + 1])}
                className="p-1.5 text-xs disabled:opacity-20 hover:text-primary"
                title={t('chat.message_item.next_branch')}
              >
                <ChevronRight size={12} />
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
});

// 提取 ApplyButton 以便單獨管理狀態
function ApplyButton({ code, language, onApply }: { code: string, language?: string, onApply: (c: string, l?: string) => Promise<boolean> }) {
  const { t } = useTranslation();
  const [status, setStatus] = React.useState<'idle' | 'loading' | 'success' | 'fail'>('idle');

  const handleClick = async () => {
    setStatus('loading');
    const success = await onApply(code, language);
    if (success) {
      setStatus('success');
      setTimeout(() => setStatus('idle'), 2000);
    } else {
      setStatus('fail');
      setTimeout(() => setStatus('idle'), 2000);
    }
  };

  return (
    <button 
      onClick={handleClick}
      disabled={status === 'loading'}
      className="flex items-center gap-1 px-2 py-0.5 rounded bg-primary/10 hover:bg-primary/20 text-primary text-xs font-bold transition-all disabled:opacity-50"
      title={t('chat.message_item.apply_to_project')}
    >
      {status === 'loading' ? <Loader2 size={10} className="animate-spin mr-1" /> : <Play size={10} fill="currentColor" />}
      {status === 'success' ? (
        <span className="text-status-success font-bold text-xs flex items-center gap-1"><Check size={10}/> OK</span>
      ) : status === 'fail' ? (
        <span className="text-status-error font-bold text-xs">FAIL</span>
      ) : "APPLY"}
    </button>
  );
}

// 設置顯示名稱以利於除錯
MessageItem.displayName = "MessageItem";
