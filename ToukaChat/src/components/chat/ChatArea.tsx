"use client";

import React from 'react';
import { useTranslation, Trans } from "react-i18next";

declare global {
  interface Window {
    vscode?: {
      postMessage: (message: Record<string, unknown>) => void;
    };
  }
}
import { Button } from '@/components/ui/button';
import { MentionInput } from '@/components/ui/MentionInput';
import Image from 'next/image';
import { PlanTracker } from "@/components/PlanTracker";
import { MessageItem } from "./MessageItem";
import { AuthSwitcher } from "./AuthSwitcher";
import { SkillSelector } from "./SkillSelector";
import { ContextMeter } from "./ContextMeter";
import { ChatWelcome } from "./ChatWelcome";
import { FeedbackReasonDialog } from "./FeedbackReasonDialog";
import { SessionPersistenceNotice } from './SessionPersistenceNotice';
import { QueuedMessageList } from "./QueuedMessageList";
import { MessageNavigator } from "./MessageNavigator";
import { useInterjectionComposer } from "@/hooks/useInterjectionComposer";
import { useFeedback } from "@/hooks/useFeedback";
import { useIsMobile } from "@/hooks/useIsMobile";
import { useMessageNavigation } from "@/hooks/useMessageNavigation";
import { MIN_NAV_TICKS } from "@/lib/chat/messageNavigation";
import { cn } from "@/lib/utils";
import {
  Paperclip, Camera, X, Folder, Activity, StopCircle, ArrowDown, Microscope, FileText, Plug
} from 'lucide-react';
import { classifyUpload, formatBytes, MAX_PASTE_INLINE_CHARS } from '@/lib/chat/uploadClassify';
import { uploadChatFile } from '@/lib/chat/uploadFile';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { 
  getLinearMessages 
} from '@/lib/tree_history';
import { useTheme } from '@/lib/ThemeContext';
import { useChat } from '@/lib/providers/ChatProvider';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { useProjects } from '@/lib/providers/ProjectProvider';
import { useModels } from '@/lib/providers/ModelProvider';
import { Virtuoso, VirtuosoHandle } from 'react-virtuoso';
import { getSystemEpoch } from '@/lib/time_utils';
import { bridgeApplyArtifact } from '@/lib/bridge';
import { logSSOTError } from '@/lib/error_handler';
import { toast } from 'sonner';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface ChatAreaProps {
  isIDE: boolean;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  handleScreenshot: () => void;
  // 這些由 page.tsx 傳遞，因為它們涉及編輯時的暫存狀態
  editingMessageId: string | null;
  setEditingMessageId: (id: string | null) => void;
  editValue: string;
  setEditValue: (val: string) => void;
}

export function ChatArea({
  isIDE, fileInputRef, handleScreenshot,
  editingMessageId, setEditingMessageId, editValue, setEditValue
}: ChatAreaProps) {
  const { t } = useTranslation();
  // 外觀打磨包：密度影響訊息間距（pb）。IDE 模式維持自身緊湊樣式。
  const { density } = useTheme();
  const compact = !isIDE && density === 'compact';

  const {
    history,
    inputValue, setInputValue,
    attachment, setAttachment,
    isSending,
    handleSend, handleStopGeneration,
    handleDeleteMessage, handleSwitchMessage, handleRegenerate,
    handleEditMessage, activeSessionId
  } = useChat();

  const { availableModels, selectedModel, setSelectedModel } = useModels();
  const { sessions, setSessions } = useSessionList();
  const { projects } = useProjects();

  // HAX G15：細粒度回饋狀態與倒讚原因彈窗。
  const { feedbackState, submit: submitFeedback } = useFeedback(activeSessionId);
  const [dislikeMsgId, setDislikeMsgId] = React.useState<string | null>(null);
  // Phase 2：文件上傳進行中（停用送出，避免在 file_ref 就緒前送出）。
  const [isUploading, setIsUploading] = React.useState(false);

  const currentSession = sessions.find(s => s.id === activeSessionId);
  const currentProject = projects.find(p => p.id === currentSession?.project_id);

  const linearMessages = React.useMemo(() => getLinearMessages(history), [history]);
  // 對話左側的訊息導覽列：手機版與 IDE 不顯示，也不訂閱捲動範圍
  const isMobile = useIsMobile();
  const navEnabled = !isIDE && !isMobile;
  const messageAreaRef = React.useRef<HTMLDivElement>(null);
  const messageNav = useMessageNavigation(linearMessages, navEnabled, messageAreaRef);

  const handleCopy = React.useCallback((text: string) => { navigator.clipboard.writeText(text); }, []);

  const handleFileChange = React.useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const { kind, reason } = classifyUpload(file);
    if (kind === "image") {
      const reader = new FileReader();
      reader.onload = (event: ProgressEvent<FileReader>) => {
        const result = event.target?.result;
        if (typeof result === 'string') {
          setAttachment({ type: "image", url: result, name: file.name, size: file.size, mime: file.type });
        }
      };
      reader.readAsDataURL(file);
    } else if (kind === "text") {
      const reader = new FileReader();
      reader.onload = (event: ProgressEvent<FileReader>) => {
        const result = event.target?.result;
        if (typeof result === 'string') {
          setAttachment({ type: "text", url: "", name: file.name, size: file.size, mime: file.type, text: result });
        }
      };
      reader.readAsText(file);
    } else if (kind === "document") {
      // 二進位/大文件 → 上傳存進當下 agent vault；附件帶 relpath，送出時注入「請 AI 用工具讀」提示。
      setIsUploading(true);
      uploadChatFile(file, activeSessionId || "")
        .then((up) => {
          setAttachment({ type: "file_ref", url: "", name: up.filename, relpath: up.relpath, size: up.size, mime: up.mime });
        })
        .catch((err) => {
          logSSOTError(err, "ChatArea:uploadChatFile");
          toast.error(t('chat.chat_area.upload_failed'));
        })
        .finally(() => setIsUploading(false));
    } else {
      toast.warning(reason === "too_large"
        ? t('chat.chat_area.upload_too_large')
        : t('chat.chat_area.upload_unsupported'));
    }
    e.target.value = ""; // 重設以便同一檔可再次選取
  }, [setAttachment, activeSessionId, t]);

  // 貼上：剪貼簿圖片→圖片附件；超長文字→收成文字附件（不灌進輸入框）。
  // 註：模型離線時 textarea 為 disabled、不會觸發 paste，故無需另 guard isModelOffline。
  const handlePaste = React.useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (items) {
      for (const it of Array.from(items)) {
        if (it.kind === "file" && it.type.startsWith("image/")) {
          const file = it.getAsFile();
          if (file) {
            e.preventDefault();
            const reader = new FileReader();
            reader.onload = (ev) => {
              const r = ev.target?.result;
              if (typeof r === 'string') {
                setAttachment({ type: "image", url: r, name: file.name || "pasted-image.png", size: file.size, mime: file.type });
              }
            };
            reader.readAsDataURL(file);
            return;
          }
        }
      }
    }
    const text = e.clipboardData?.getData("text");
    if (text && text.length > MAX_PASTE_INLINE_CHARS) {
      e.preventDefault();
      setAttachment({
        type: "text", url: "", text,
        name: t('chat.chat_area.pasted_text_filename'),
        size: new Blob([text]).size, mime: "text/plain",
      });
      toast.success(t('chat.chat_area.pasted_as_file'));
    }
  }, [setAttachment, t]);

  const handleApplyCode = React.useCallback(async (code: string, language?: string) => {
    try {
      return await bridgeApplyArtifact({
        content: code,
        title: `inline_code_${getSystemEpoch()}`,
        type: 'code',
        language: language || 'txt',
        proxyUrl: getActiveProxyUrl(),
        fetchTouka
      });
    } catch (e) {
      logSSOTError(e, "ChatArea:handleApplyCode");
      return false;
    }
  }, []);

  const virtuosoRef = React.useRef<VirtuosoHandle>(null);

  // --- Auto-scroll to bottom ---
  const messagesCount = linearMessages.length;

  // 當正在發送（流式輸出）時，確保虛擬列表跟隨底部
  React.useEffect(() => {
    if (isSending && virtuosoRef.current) {
      virtuosoRef.current.scrollToIndex({
        index: linearMessages.length - 1,
        behavior: 'auto',
      });
    }
  }, [messagesCount, isSending]);

  // 組件掛載時滾動到底部
  React.useEffect(() => {
    const timer = setTimeout(() => {
      virtuosoRef.current?.scrollToIndex({
        index: linearMessages.length - 1,
        behavior: 'auto',
      });
    }, 200);
    return () => clearTimeout(timer);
  }, [messagesCount]);

  const [showScrollBottom, setShowScrollBottom] = React.useState(false);

  const handleScroll = (atBottom: boolean) => {
    setShowScrollBottom(!atBottom);
  };

  const manualScrollToBottom = () => {
    virtuosoRef.current?.scrollToIndex({
      index: linearMessages.length - 1,
      behavior: 'smooth',
    });
  };

  const jumpToMessage = (index: number) => {
    virtuosoRef.current?.scrollToIndex({ index, align: 'start', behavior: 'smooth' });
  };

  const selectedModelInfo = availableModels.find(m => m.id === selectedModel);
  const isModelOffline = selectedModelInfo?.status === 'offline';
  // ADR-0249：生成中 Enter＝排隊、Ctrl+Enter＝優先、Esc＝停止；回合結束時未送達的自動送出。
  const composer = useInterjectionComposer({
    sessionId: activeSessionId, isSending, disabled: isModelOffline, inputValue, setInputValue,
    send: handleSend, stop: handleStopGeneration,
  });

  // HAX G14（H3）：切換模型即提示「回應風格可能改變」——讓行為變動對使用者透明（謹慎更新）。
  const handleModelChange = React.useCallback((id: string) => {
    setSelectedModel(id);
    const m = availableModels.find(x => x.id === id);
    toast.info(t('chat.chat_area.model_switched_style_may_change', { model: m?.name || id }));
  }, [setSelectedModel, availableModels, t]);

  // ADR-0087：深入研究切換＝**逐則**（per-message）語意——旗標僅存於記憶體內的
  // 當前 session（切走／重整歸零），語意上每則昂貴的多步研究都由使用者顯式開關。
  // P4（ADR-0161）：移除舊 syncSession 死碼——後端 SessionModel 無 deep_research 欄位、
  // POST 被 Pydantic 靜默丟棄從未落盤（徒發無效請求）；且持久化黏著違背 ADR-0087
  // 「顯式切換以避免忘了關→之後每則靜默觸發昂貴研究」的初衷。送出當次仍正確帶旗標
  // （connections.ts body.deep_research，逐則語意本就完整）。
  const isDeepResearch = !!currentSession?.deepResearch;
  const toggleDeepResearch = React.useCallback(() => {
    if (!activeSessionId) return;
    setSessions(prev => prev.map(s =>
      s.id === activeSessionId ? { ...s, deepResearch: !s.deepResearch } : s
    ));
  }, [activeSessionId, setSessions]);

  // ADR-0161：MCP 動態掛載切換＝逐則旗標（同 deepResearch 語意）——is_system 系統技能
  // 移出技能選單後，此為「MCP 動態掛載模式」（漸進揭露＋connect_mcp_server）的使用者面
  // 唯一啟用入口。旗標經 body.mcp_dispatcher 逐則帶到後端 tool.py。
  const isMcpDispatcher = !!currentSession?.mcpDispatcher;
  const toggleMcpDispatcher = React.useCallback(() => {
    if (!activeSessionId) return;
    setSessions(prev => prev.map(s =>
      s.id === activeSessionId ? { ...s, mcpDispatcher: !s.mcpDispatcher } : s
    ));
  }, [activeSessionId, setSessions]);

  return (
    <div className={cn("h-full flex flex-col mx-auto w-full relative", isIDE ? "max-w-none" : "max-w-[95%] 2xl:max-w-[85%]")}>
      <SessionPersistenceNotice key={activeSessionId} sessionId={activeSessionId} />
      {/* 訊息捲動區：導覽列以它為定位基準，不會壓到下方輸入區 */}
      <div ref={messageAreaRef} className="relative flex-1 min-h-0">
      <Virtuoso
        ref={virtuosoRef}
        data={linearMessages}
        atBottomStateChange={handleScroll}
        rangeChanged={messageNav.onRangeChanged}
        initialTopMostItemIndex={Math.max(0, linearMessages.length - 1)}
        followOutput={(isAtBottom) => {
          return isAtBottom ? 'auto' : false;
        }}
        className={cn(
          "h-full custom-scrollbar relative overflow-x-hidden",
          isIDE ? "p-3" : "py-4"
        )}
        components={{
          Header: () => (
            <div className="flex flex-col gap-4 mb-4">
              {currentProject && (
                <div className="flex items-center gap-2 p-3 bg-primary/5 border border-primary/20 rounded-xl text-primary font-medium text-sm">
                  <Folder size={18} />
                  <span>
                    <Trans
                      i18nKey="chat.chat_area.conversation_linked_project"
                      values={{ name: currentProject.name }}
                      components={{ 1: <span className="font-bold underline" /> }}
                    />
                  </span>
                  <div className="ml-auto text-xs opacity-70 px-2 py-0.5 bg-primary/10 rounded-full border border-primary/10 uppercase tracking-widest">
                    Project Context Active
                  </div>
                </div>
              )}

              {isModelOffline && (
                <div className="flex items-center gap-2 p-3 bg-status-warning/10 border border-status-warning/20 rounded-xl text-status-warning font-medium text-sm">
                  <Activity size={18} className="animate-pulse" />
                  <span>
                    <Trans
                      i18nKey="chat.chat_area.model_offline_history_only"
                      values={{ model: selectedModelInfo?.name || selectedModel }}
                      components={{ 1: <span className="font-bold" /> }}
                    />
                  </span>
                </div>
              )}
              
              {linearMessages.length === 0 && (
                // HAX G1：空狀態揭露能力＋可點擊範例提示（點擊即填入輸入框）。
                <ChatWelcome isIDE={isIDE} onPickPrompt={setInputValue} />
              )}
            </div>
          ),
          Footer: () => (
            <div className="flex flex-col gap-4">
               <PlanTracker />
               <div className="h-8 w-full" />
            </div>
          )
        }}
        itemContent={(index, msg) => (
          <div className={cn("w-full min-w-0 overflow-hidden", isIDE ? "px-3 pb-2" : compact ? "px-6 pb-4" : "px-6 pb-8")}>
            <MessageItem 
              key={msg.id}
              msg={msg}
              history={history}
              isSending={isSending}
              isIDE={isIDE}
              editingMessageId={editingMessageId}
              setEditingMessageId={setEditingMessageId}
              editValue={editValue}
              setEditValue={setEditValue}
              handleCopy={handleCopy}
              handleEditMessage={handleEditMessage}
              handleDeleteMessage={handleDeleteMessage}
              handleRegenerate={handleRegenerate}
              handleSwitchMessage={handleSwitchMessage}
              handleApplyCode={handleApplyCode}
              feedback={feedbackState[msg.id]}
              onFeedback={submitFeedback}
              onRequestDislike={setDislikeMsgId}
            />
          </div>
        )}
      />
      {navEnabled && messageNav.ticks.length >= MIN_NAV_TICKS && (
        <MessageNavigator
          ticks={messageNav.ticks}
          currentIndex={messageNav.currentIndex}
          onJump={jumpToMessage}
        />
      )}
      </div>

      {/* 回到最底按鈕 */}
      {showScrollBottom && (
        <div className="absolute right-8 bottom-32 z-50 animate-in fade-in zoom-in duration-300">
          <div className="absolute -inset-0.5 bg-gradient-to-r from-primary to-primary/80 rounded-full blur opacity-40 animate-pulse"></div>
          <Button 
            variant="secondary"
            size="icon"
            onClick={manualScrollToBottom}
            className="relative bg-background border border-border rounded-full shadow-xl text-primary hover:scale-110 transition-all group h-10 w-10"
            title={t('chat.chat_area.scroll_to_bottom')}
          >
            <div className="relative flex items-center justify-center">
              <ArrowDown size={20} />
              <span className="absolute -top-1 -right-1 w-1.5 h-1.5 bg-primary rounded-full animate-pulse"></span>
            </div>
          </Button>
        </div>
      )}

      <div className={cn(
        "bg-transparent shrink-0",
        isIDE ? "p-3" : "p-6"
      )}>
        <div className="flex justify-end mb-1 px-1">
          <ContextMeter messages={linearMessages} />
        </div>
        <div className="relative group">
          <div className={cn(
             "absolute -inset-0.5 bg-gradient-to-r from-primary to-primary/80 rounded-2xl blur transition duration-500",
             isModelOffline ? "opacity-10" : "opacity-20 group-focus-within:opacity-40"
          )}></div>
          <div className={cn(
            "relative border border-border rounded-2xl bg-background overflow-hidden transition-all",
            isModelOffline ? "opacity-60 cursor-not-allowed" : "focus-within:border-primary"
          )}>
            {attachment && (
              <div className="px-3 pt-3 flex items-center gap-2">
                {attachment.type === "image" ? (
                  <div className="relative group/thumb rounded-lg overflow-hidden w-16 h-16 border border-border shadow-sm bg-muted/50">
                    <Image
                      src={attachment.url}
                      alt="preview"
                      fill
                      className="object-cover"
                      unoptimized
                    />
                    {/* WCAG 2.5.8（A4）：點擊區 ≥24px（p-1.5×2 + icon12） */}
                    <button onClick={() => setAttachment(null)} aria-label={t('chat.chat_area.remove_attachment')} className="absolute top-0.5 right-0.5 p-1.5 bg-status-error text-white rounded-full hover:bg-status-error/80 shadow-lg"><X size={12} /></button>
                  </div>
                ) : (
                  <div className="relative flex items-center gap-2 max-w-xs rounded-lg border border-border bg-muted/50 px-3 py-2">
                    <FileText size={16} className="text-muted-foreground shrink-0" />
                    <div className="min-w-0">
                      <div className="text-xs font-medium truncate">{attachment.name}</div>
                      {typeof attachment.size === "number" && (
                        <div className="text-xs text-muted-foreground">{formatBytes(attachment.size)}</div>
                      )}
                    </div>
                    <button onClick={() => setAttachment(null)} aria-label={t('chat.chat_area.remove_attachment')} className="ml-1 p-1.5 text-muted-foreground hover:text-status-error rounded-full"><X size={12} /></button>
                  </div>
                )}
              </div>
            )}
            <QueuedMessageList items={composer.items} onRetract={composer.retract} onEdit={composer.edit} />
            <MentionInput
              value={inputValue}
              onChange={e => !isModelOffline && setInputValue(e.target.value)}
              onPaste={handlePaste}
              onKeyDown={composer.onKeyDown}
              placeholder={isModelOffline ? t('chat.chat_area.placeholder_model_offline') : isSending ? t('chat.interjection.generating_hint') : (isIDE ? t('chat.chat_area.placeholder_enter_command') : t('chat.chat_area.placeholder_enter_command_or_chat'))}
              disabled={isModelOffline}
              className={cn(
                "w-full border-0 focus-visible:ring-0 resize-none px-4 pt-3 pb-2 text-sm shadow-none bg-transparent",
                isIDE ? "h-16" : "h-28",
                isModelOffline && "cursor-not-allowed"
              )}
            />
            <div className="flex items-center justify-between px-3 py-2 bg-transparent border-t border-border/30">
              <div className="flex items-center gap-1">
                <input type="file" ref={fileInputRef} className="hidden" accept="image/*,text/*,.md,.markdown,.csv,.tsv,.json,.log,.xml,.yml,.yaml,.toml,.ini,.html,.css,.js,.jsx,.ts,.tsx,.py,.go,.rs,.java,.sh,.sql,.vue,.svelte,.pdf,.docx,.xlsx,.pptx" onChange={handleFileChange} />
                <Button 
                  variant="ghost" 
                  size="icon"
                  onClick={() => !isModelOffline && fileInputRef.current?.click()} 
                  className={cn("h-8 w-8 text-muted-foreground", !isModelOffline && "hover:text-primary hover:bg-primary/10")} 
                  title={t('chat.chat_area.upload_file')}
                  disabled={isModelOffline}
                >
                  <Paperclip size={18} />
                </Button>
                
                {!isIDE && (
                  <Button 
                    variant="ghost" 
                    size="icon"
                    onClick={() => !isModelOffline && handleScreenshot()} 
                    className={cn("h-8 w-8 text-muted-foreground", !isModelOffline && "hover:text-primary hover:bg-primary/10")} 
                    title={t('chat.chat_area.screenshot')}
                    disabled={isModelOffline}
                  >
                    <Camera size={18} />
                  </Button>
                )}
                
                <div className="w-px h-4 bg-border/60 mx-1"></div>
                <AuthSwitcher />
                <SkillSelector />
                {/* ADR-0087：深入研究切換鈕（逐則開關，開啟即對「這一則問題」做多步搜尋＋引用報告） */}
                <button
                  type="button"
                  onClick={toggleDeepResearch}
                  aria-pressed={isDeepResearch}
                  title={t('chat.chat_area.deep_research_tooltip')}
                  className={cn(
                    "h-7 px-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider rounded-lg border transition-all",
                    isDeepResearch
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border/50 bg-background/50 text-muted-foreground hover:text-primary"
                  )}
                >
                  <Microscope size={12} className="shrink-0" />
                  <span>{t('chat.chat_area.research')}</span>
                </button>
                {/* ADR-0161：MCP 動態掛載切換鈕（逐則開關，啟用漸進揭露＋connect_mcp_server） */}
                <button
                  type="button"
                  onClick={toggleMcpDispatcher}
                  aria-pressed={isMcpDispatcher}
                  title={t('chat.chat_area.mcp_dispatcher_tooltip')}
                  className={cn(
                    "h-7 px-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider rounded-lg border transition-all",
                    isMcpDispatcher
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border/50 bg-background/50 text-muted-foreground hover:text-primary"
                  )}
                >
                  <Plug size={12} className="shrink-0" />
                  <span>{t('chat.chat_area.mcp_dispatcher')}</span>
                </button>
                <div className="w-px h-4 bg-border/60 mx-1"></div>
                
                <Select value={selectedModel} onValueChange={handleModelChange}>
                  <SelectTrigger aria-label={t('chat.chat_area.select_model')} className="h-8 bg-transparent border-none text-xs font-bold uppercase focus:ring-0 focus:ring-offset-0 cursor-pointer text-muted-foreground hover:text-primary hover:bg-primary/5 transition-all shadow-none px-2 w-auto gap-2">
                    <SelectValue placeholder={t('chat.chat_area.select_model')} />
                  </SelectTrigger>
                  <SelectContent className="bg-popover border-border">
                    {availableModels.map(m => (
                      <SelectItem 
                        key={m.id} 
                        value={m.id} 
                        className={cn(m.status === 'offline' && "text-muted-foreground opacity-70")}
                      >
                        <div className="flex items-center gap-2">
                          {m.status === 'offline' && <span className="text-xs">🚫</span>}
                          <span>{m.name || m.id}</span>
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              
              <div className="flex items-center gap-2">
                {/* WCAG 4.1.3：串流開始/完成的 sr-only 宣告器——逐 token 不轟炸 SR，
                    僅在狀態切換時宣告（aria-live 不唸初始渲染、只唸後續變化） */}
                <div role="status" className="sr-only">
                  {isSending ? t('chat.chat_area.generating_reply') : t('chat.chat_area.reply_completed')}
                </div>
                {isSending && (
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={composer.stop}
                    className="h-8 w-8 text-status-error hover:bg-status-error/10"
                    title={t('chat.chat_area.stop')}
                  >
                    <StopCircle size={20} />
                  </Button>
                )}
                <Button 
                  onClick={() => (isSending ? composer.queueInput() : handleSend())} 
                  disabled={isModelOffline || isUploading || (isSending ? !inputValue.trim() : !inputValue.trim() && !attachment)}
                  size="sm" 
                  className={cn(
                    "rounded-xl font-bold transition-all active:scale-95", 
                    isIDE ? "px-4 h-8 text-xs" : "px-6"
                  )}
                >
                  {isSending ? t('chat.interjection.queue_button') : (isModelOffline ? t('chat.chat_area.model_offline_button') : t('chat.chat_area.send'))}
                </Button>
              </div>
            </div>
          </div>
        </div>
        {/* HAX G2：常駐免責提示——讓使用者知道系統「做得多好」的邊界。 */}
        <p className="mt-2 text-center text-xs text-muted-foreground select-none">
          {t('chat.chat_area.ai_disclaimer')}
        </p>
      </div>

      {/* HAX G15/G6：倒讚原因彈窗（選填，兼有害內容回報）。 */}
      <FeedbackReasonDialog
        open={dislikeMsgId !== null}
        onSubmit={(reason) => {
          if (dislikeMsgId) submitFeedback(dislikeMsgId, 'down', reason);
          setDislikeMsgId(null);
        }}
        onCancel={() => setDislikeMsgId(null)}
      />
    </div>
  );
}
