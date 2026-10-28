"use client";

import { useState, useRef, useEffect, Suspense } from 'react';
import LeftSidebar from '@/components/LeftSidebar';
import RightSidebar from '@/components/RightSidebar';
import { SettingsView } from '@/components/settings/SettingsView';
import { PulseDashboard } from "@/components/dashboard/PulseDashboard";
import { VaultDashboard } from "@/components/dashboard/VaultDashboard";
import { SoulStudio } from "@/components/layout/SoulStudio";
import { ModelStudio } from "@/components/layout/ModelStudio";
import { ProjectsView } from "@/components/projects/ProjectsView";
import { useArtifacts } from "@/hooks/useArtifacts";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { cn } from "@/lib/utils";
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { ChatArea } from '@/components/chat/ChatArea';
import { MainAreaControls } from '@/components/layout/MainAreaControls';
import { SetupNoticeDot } from '@/components/setup/SetupNoticeDot';
import { useHasSetupNotice } from '@/components/setup/SetupNoticesProvider';
import {
  Settings, MessageSquare,  BarChart4,
  Menu, Loader2
} from 'lucide-react';
import { Panel, PanelGroup, PanelResizeHandle, ImperativePanelHandle } from "react-resizable-panels";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { logSSOTError } from "@/lib/error_handler";
import { useChat } from '@/lib/providers/ChatProvider';
import { useModels } from '@/lib/providers/ModelProvider';
import { useDebug } from '@/lib/providers/DebugProvider';
import { postToBridge, isIDE } from '@/lib/bridge';
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";

interface VSCodeAPI {
  postMessage: (message: Record<string, unknown>) => void;
}

declare global {
  interface Window {
    vscode?: VSCodeAPI;
  }
}

export default function ChatPage() {
  return (
    <Suspense fallback={
      <div className="flex h-screen w-screen flex-col items-center justify-center bg-background text-foreground gap-3">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
        <div className="text-xs font-black tracking-widest text-muted-foreground uppercase animate-pulse">Initializing Touka...</div>
      </div>
    }>
      <ChatLobby />
    </Suspense>
  );
}

function ChatLobby() {
  const { t } = useTranslation();
  const isMobile = useMediaQuery("(max-width: 768px)");
  const { activeSessionId, setAttachment, handleNewSession } = useChat();
  const { selectedModel } = useModels();

  const { isOpen: isRightSidebarOpen, setIsOpen: setRightSidebarOpen, setActiveTab } = useArtifacts();
  const { isSimulationMode } = useDebug();
  const hasSetupNotice = useHasSetupNotice();
  
  // 視圖狀態持久化：在掛載後讀取以避免 Hydration Mismatch
  const [viewMode, setViewMode] = useState<"chat" | "soul" | "studio" | "pulse" | "vault" | "settings" | "projects">("chat");
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useLocalStorage("touka_sidebar_collapsed", false);

  // 客戶端掛載後的副作用處理
  useEffect(() => {
    // 讀取網址參數
    const params = new URLSearchParams(window.location.search);
    const v = params.get('view');
    const validViews = ["chat", "soul", "studio", "pulse", "vault", "settings", "projects"];
    if (v && validViews.includes(v)) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setViewMode(v as typeof viewMode);
    }

  }, []);
  
  const leftPanelRef = useRef<ImperativePanelHandle>(null);
  const rightPanelRef = useRef<ImperativePanelHandle>(null);

  useEffect(() => {
    postToBridge({
      type: 'updateModel',
      model: selectedModel
    });
  }, [selectedModel]);

  // 將當前視圖與對話 ID 同步到網址列 (方案 A)
  // 加入 isInitialLoading 檢查，避免在 Provider 還沒載入清單前就刪掉網址參數
  const [isInitialLoading, setIsInitialLoading] = useState(true);
  useEffect(() => {
    const timer = setTimeout(() => setIsInitialLoading(false), 1000);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (typeof window !== 'undefined') {
        const url = new URL(window.location.href);
        let changed = false;
        
        // 同步 ViewMode
        if (url.searchParams.get('view') !== viewMode) {
            url.searchParams.set('view', viewMode);
            changed = true;
        }
        
        // 同步 SessionID
        if (activeSessionId) {
            if (url.searchParams.get('session') !== activeSessionId) {
                url.searchParams.set('session', activeSessionId);
                changed = true;
            }
        } else if (!isInitialLoading) {
            // 只有在非初始化階段且真的沒有 activeSessionId 時才刪除
            if (url.searchParams.has('session')) {
                url.searchParams.delete('session');
                changed = true;
            }
        }
        
        if (changed) {
            window.history.replaceState(null, '', url.toString());
        }

        // 強制全局導航同步 (確保側邊欄等組件共用同一個狀態)
        window.dispatchEvent(new CustomEvent('touka_view_synced', { detail: viewMode }));
    }
  }, [viewMode, activeSessionId, isInitialLoading, setViewMode]);
  
  useEffect(() => {
    const handleViewSwitch = ((e: CustomEvent<string>) => {
      if (e.detail === 'chat') setViewMode('chat');
      else if (e.detail === 'projects') setViewMode('projects');
      else if (e.detail === 'soul') setViewMode('soul');
    }) as EventListener;
    window.addEventListener('touka_switch_view', handleViewSwitch);
    return () => window.removeEventListener('touka_switch_view', handleViewSwitch);
  }, []);
  
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

  // 自動開啟偵錯邊欄連動 ⭐
  useEffect(() => {
    if (isSimulationMode) {
      setRightSidebarOpen(true);
      setActiveTab('debug');
    }
  }, [isSimulationMode, setRightSidebarOpen, setActiveTab]);

  // 智慧滾動由 ChatArea 內部的 Virtuoso 組件自行處理

  const handleScreenshot = async () => {
    // P4（ADR-0161）：兩條失敗路徑不再靜默——舊制 API 缺席直接 return、catch 全吞，
    // 使用者點相機毫無回饋＝體感「按鈕壞了」。API 缺席明示不支援；使用者主動取消
    // （NotAllowedError）不打擾，其餘擷取錯誤 fail-loud。
    if (!navigator.mediaDevices?.getDisplayMedia) {
      toast.warning(t('chat.chat_area.screenshot_unsupported'));
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: true });
      const video = document.createElement('video');
      video.srcObject = stream;
      video.onloadedmetadata = () => {
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext('2d')?.drawImage(video, 0, 0);
        setAttachment({ type: 'image', url: canvas.toDataURL('image/png') });
        stream.getTracks().forEach(track => track.stop());
      };
      await video.play();
    } catch (e) {
      // 使用者在系統分享對話框按「取消／拒絕」＝NotAllowedError，屬正常操作、不擾。
      if (e instanceof DOMException && e.name === 'NotAllowedError') return;
      logSSOTError(e, 'page:handleScreenshot');
      toast.error(t('chat.chat_area.screenshot_failed'));
    }
  };


  // 桌面版左側邊欄展開／收合：以面板實際狀態為準；面板已是目標狀態（記住的狀態沒同步到）時只校正狀態，
  // 免得按了沒反應。
  const setLeftSidebarOpen = (open: boolean) => {
    const panel = leftPanelRef.current;
    if (!panel) return;
    if (open && panel.isCollapsed()) panel.expand();
    else if (!open && !panel.isCollapsed()) panel.collapse();
    else setIsSidebarCollapsed(!open);
  };

  const handleToggleRightSidebar = () => {
    const next = !isRightSidebarOpen;
    setRightSidebarOpen(next);
  };

  useEffect(() => {
    if (rightPanelRef.current) {
      if (isRightSidebarOpen) rightPanelRef.current.expand();
      else rightPanelRef.current.collapse();
    }
  }, [isRightSidebarOpen]);

  // WCAG 2.1.1（A2）：行動版側欄開啟時，Esc＝關閉（背景遮罩點擊的鍵盤替代）
  useEffect(() => {
    if (!isMobile || (isSidebarCollapsed && !isRightSidebarOpen)) return;
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setIsSidebarCollapsed(true);
        setRightSidebarOpen(false);
      }
    };
    window.addEventListener('keydown', onEscape);
    return () => window.removeEventListener('keydown', onEscape);
  }, [isMobile, isSidebarCollapsed, isRightSidebarOpen]);

  // HAX G7（H3）：全域鍵盤捷徑 ⌘/Ctrl+Shift+O 開新對話（刻意不搶瀏覽器 Ctrl+N／Ctrl+T）。
  useEffect(() => {
    const onNewChat = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'o' || e.key === 'O')) {
        e.preventDefault();
        handleNewSession();
        setViewMode('chat');
      }
    };
    window.addEventListener('keydown', onNewChat);
    return () => window.removeEventListener('keydown', onNewChat);
  }, [handleNewSession, setViewMode]);


  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground relative font-sans">
      {/* Mobile Backdrop（WCAG 2.1.1：裝飾性遮罩 aria-hidden；鍵盤替代＝下方 Esc 監聽） */}
      {isMobile && (!isSidebarCollapsed || isRightSidebarOpen) && (
        <div
          aria-hidden="true"
          className="fixed inset-0 bg-black/40 backdrop-blur-sm z-[40] transition-opacity duration-300"
          onClick={() => {
            setIsSidebarCollapsed(true);
            setRightSidebarOpen(false);
          }}
        />
      )}

      {isMobile ? (
        <main id="main-content" className="flex-1 flex flex-col min-w-0 relative h-full">
          <header className={cn("border-b border-border flex items-center justify-between shrink-0 bg-background z-20 h-16 px-4")}>
             <div className="flex items-center gap-1">
                <Sheet open={!isSidebarCollapsed} onOpenChange={(open) => setIsSidebarCollapsed(!open)}>
                  <SheetTrigger asChild>
                    <button 
                      className="relative p-2 -ml-2 text-muted-foreground hover:text-primary transition-colors shrink-0"
                      aria-label={hasSetupNotice ? t('sidebar.setup_notice.open_menu_aria') : undefined}
                    >
                      <Menu size={24} />
                      <SetupNoticeDot />
                    </button>
                  </SheetTrigger>
                  <SheetContent side="left" className="p-0 w-72 border-none shadow-2xl bg-sidebar">
                    <SheetTitle className="sr-only">{t('app.home_page.nav_menu_title')}</SheetTitle>
                    <LeftSidebar
                      onChatClick={() => { setViewMode("chat"); setIsSidebarCollapsed(true); }}
                      onSoulClick={() => { setViewMode("soul"); setIsSidebarCollapsed(true); }}
                      onStudioClick={() => { setViewMode("studio"); setIsSidebarCollapsed(true); }}
                      onPulseClick={() => { setViewMode("pulse"); setIsSidebarCollapsed(true); }}
                      onVaultClick={() => { setViewMode("vault"); setIsSidebarCollapsed(true); }}
                      onSettingsClick={() => { setViewMode("settings"); setIsSidebarCollapsed(true); }}
                      onProjectsClick={() => { setViewMode("projects"); setIsSidebarCollapsed(true); }}
                      onSetupAction={() => setIsSidebarCollapsed(true)}
                      activeMode={viewMode}
                      isCollapsed={false}
                    />
                  </SheetContent>
                </Sheet>
                <div className="flex items-center gap-2 ml-2">
                  <MessageSquare className="text-primary shrink-0" size={24} />
                  <h1 className="text-lg font-black tracking-tight uppercase italic truncate">Touka</h1>
                </div>
             </div>
              <div className="flex items-center gap-2">
                <button onClick={() => setViewMode("settings")} className={cn("p-1.5 rounded-lg transition-colors", viewMode === "settings" ? "bg-primary/10 text-primary" : "text-muted-foreground")}><Settings size={20} /></button>
                <button onClick={handleToggleRightSidebar} className={cn("p-1.5 rounded-lg transition-colors", isRightSidebarOpen ? "bg-primary/10 text-primary" : "text-muted-foreground")}>
                  <BarChart4 size={20} />
                </button>
              </div>
          </header>

          <div className="flex-1 overflow-hidden relative">
            {viewMode === "chat" ? (
              <ChatArea
                isIDE={isIDE()}
                fileInputRef={fileInputRef}
                handleScreenshot={handleScreenshot}
                editingMessageId={editingMessageId}
                setEditingMessageId={setEditingMessageId}
                editValue={editValue}
                setEditValue={setEditValue}
              />
            ) : viewMode === "soul" ? <SoulStudio /> : viewMode === "studio" ? <ModelStudio /> : viewMode === "pulse" ? <PulseDashboard /> : viewMode === "vault" ? <VaultDashboard /> : viewMode === "projects" ? <ProjectsView /> : <SettingsView />}
          </div>

          <Sheet open={isRightSidebarOpen} onOpenChange={setRightSidebarOpen}>
            <SheetContent side="right" className="p-0 w-80 border-none shadow-2xl bg-sidebar">
              <SheetTitle className="sr-only">{t('app.home_page.tool_panel_title')}</SheetTitle>
              <RightSidebar isIDE={isIDE()} isOpen={isRightSidebarOpen} onClose={() => setRightSidebarOpen(false)} />
            </SheetContent>
          </Sheet>
        </main>
      ) : (
        <PanelGroup direction="horizontal" id="main-layout-flat" autoSaveId="touka-main-layout-v2-flat">
          {!isIDE() && (
            <>
              <Panel 
                ref={leftPanelRef}
                order={1}
                id="left-sidebar-panel"
                defaultSize={isSidebarCollapsed ? 0 : 18} 
                minSize={12} 
                collapsible
                onCollapse={() => setIsSidebarCollapsed(true)}
                onExpand={() => setIsSidebarCollapsed(false)}
                className="flex flex-col overflow-hidden"
              >
                <LeftSidebar
                  activeMode={viewMode}
                  onChatClick={() => setViewMode('chat')}
                  onSoulClick={() => setViewMode('soul')}
                  onStudioClick={() => setViewMode('studio')}
                  onPulseClick={() => setViewMode('pulse')}
                  onVaultClick={() => setViewMode('vault')}
                  onSettingsClick={() => setViewMode('settings')}
                  onProjectsClick={() => setViewMode('projects')}
                  onCollapse={() => setLeftSidebarOpen(false)}
                  isCollapsed={isSidebarCollapsed}
                />
              </Panel>
              <PanelResizeHandle className={cn("w-0.5 bg-border hover:bg-primary/50 transition-colors z-30", isSidebarCollapsed && "hidden")} />
            </>
          )}

          <Panel id="main-content" order={2} minSize={30} className="flex flex-col min-w-0 relative overflow-hidden">
            <main className="flex-1 flex flex-col min-w-0 relative h-full">
              <MainAreaControls
                isIDE={isIDE()}
                isLeftSidebarCollapsed={isSidebarCollapsed}
                onExpandLeftSidebar={() => setLeftSidebarOpen(true)}
                isRightSidebarOpen={isRightSidebarOpen}
                onToggleRightSidebar={handleToggleRightSidebar}
                isSettingsOpen={viewMode === "settings"}
                onToggleSettings={() => setViewMode(viewMode === "settings" ? "chat" : "settings")}
              />

              <div className="flex-1 overflow-hidden relative">
                {viewMode === "chat" ? (
                  <ChatArea
                    isIDE={isIDE()}
                    fileInputRef={fileInputRef}
                    handleScreenshot={handleScreenshot}
                    editingMessageId={editingMessageId}
                    setEditingMessageId={setEditingMessageId}
                    editValue={editValue}
                    setEditValue={setEditValue}
                  />
                ) : viewMode === "soul" ? <SoulStudio /> : viewMode === "studio" ? <ModelStudio /> : viewMode === "pulse" ? <PulseDashboard /> : viewMode === "vault" ? <VaultDashboard /> : viewMode === "projects" ? <ProjectsView /> : <SettingsView />}
              </div>
            </main>
          </Panel>

          <PanelResizeHandle className={cn("w-0.5 bg-border hover:bg-primary/50 transition-colors z-30", !isRightSidebarOpen && "hidden")} />

          <Panel 
            ref={rightPanelRef}
            order={3}
            id="right-sidebar-panel"
            collapsible
            defaultSize={0} 
            minSize={15} 
            maxSize={60}
            onCollapse={() => setRightSidebarOpen(false)}
            onExpand={() => setRightSidebarOpen(true)}
            className="flex flex-col overflow-hidden"
          >
            <RightSidebar isIDE={isIDE()} isOpen={isRightSidebarOpen} onClose={handleToggleRightSidebar} />
          </Panel>
        </PanelGroup>
      )}
    </div>
  );
}
