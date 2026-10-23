import { useTranslation } from "react-i18next";
import {
  Menu, MessageSquare, Folder, User, Wrench,
  Activity, Database, Settings, BarChart4,
  LucideIcon
} from "lucide-react";
import { cn } from "@/lib/utils";
import { SetupNoticeDot } from "@/components/setup/SetupNoticeDot";
import { useHasSetupNotice } from "@/components/setup/SetupNoticesProvider";

interface MainHeaderProps {
  viewMode: "chat" | "soul" | "studio" | "pulse" | "vault" | "settings" | "projects";
  setViewMode: (v: "chat" | "soul" | "studio" | "pulse" | "vault" | "settings" | "projects") => void;
  isIDE: boolean;
  isMobile: boolean;
  onToggleSidebar: () => void;
  isRightSidebarOpen: boolean;
  onToggleRightSidebar: () => void;
  sysStatus: { mode: string };
}


// 共享的導覽按鈕內部渲染邏輯 (移至外部以遵守 React 規則)
const NavButton = ({ 
  mode, 
  activeMode,
  icon: Icon, 
  label,
  onClick
}: { 
  mode: MainHeaderProps["viewMode"]; 
  activeMode: MainHeaderProps["viewMode"];
  icon: LucideIcon; 
  label: string;
  onClick: (m: MainHeaderProps["viewMode"]) => void;
}) => (
  <button
    onClick={() => onClick(mode)}
    // WCAG 4.1.2（A5 axe）：label 在 <xl 寬度時視覺隱藏 → 提供恆存的可及名稱
    aria-label={label}
    aria-current={activeMode === mode ? 'true' : undefined}
    className={cn(
      "px-3 py-1.5 text-sm font-bold rounded-lg transition-all flex items-center gap-2 min-w-0 flex-shrink-0 lg:flex-shrink",
      activeMode === mode
        ? "bg-background shadow-sm text-primary"
        : "text-muted-foreground hover:text-foreground hover:bg-background/50"
    )}
  >
    <Icon size={16} className="flex-shrink-0" /> 
    <span className="hidden xl:inline truncate">{label}</span>
  </button>
);

export function MainHeader({
  viewMode,
  setViewMode,
  isIDE,
  isMobile,
  onToggleSidebar,
  isRightSidebarOpen,
  onToggleRightSidebar,
  sysStatus
}: MainHeaderProps) {
  const { t } = useTranslation();
  const hasSetupNotice = useHasSetupNotice();

  const handleNavClick = (mode: MainHeaderProps["viewMode"]) => setViewMode(mode);

  return (
    <header className={cn(
      "border-b border-border flex items-center justify-between shrink-0 bg-background z-20", 
      isIDE ? "h-10 px-0" : "h-16 px-4 lg:px-6"
    )}>
      {/* Left Section: Menu & Title */}
      <div className={cn("flex items-center min-w-0", isIDE ? "gap-2 px-3 border-r border-border h-full bg-sidebar/50" : "gap-1")}>
        {!isIDE && (
          <button 
            onClick={onToggleSidebar}
            className="relative p-2 -ml-2 text-muted-foreground hover:text-primary transition-colors shrink-0"
            aria-label={hasSetupNotice ? t('sidebar.setup_notice.toggle_sidebar_aria') : t('layout.main_header.toggle_sidebar')}
          >
            <Menu size={24} />
            <SetupNoticeDot />
          </button>
        )}
        <div className={cn("flex items-center gap-2 lg:gap-3 min-w-0", !isIDE && "ml-2 lg:ml-6")}>
          {viewMode === 'projects' ? (
            <Folder className="text-primary shrink-0" size={isIDE ? 16 : 24} />
          ) : (
            <MessageSquare className="text-primary shrink-0" size={isIDE ? 16 : 24} />
          )}
          {!isIDE && (
            <h1 className="text-lg lg:text-xl font-black tracking-tight uppercase italic truncate">
              <span className="hidden lg:inline mr-2">Touka</span> 
              {viewMode === 'chat' && t('layout.main_header.views.chat')}
              {viewMode === 'soul' && t('layout.main_header.views.soul')}
              {viewMode === 'studio' && t('layout.main_header.views.studio')}
              {viewMode === 'pulse' && t('layout.main_header.views.pulse')}
              {viewMode === 'vault' && t('layout.main_header.views.vault')}
              {viewMode === 'projects' && t('layout.main_header.views.projects')}
              {viewMode === 'settings' && t('layout.main_header.views.settings')}
            </h1>
          )}
          {isIDE && <span className="text-xs font-bold uppercase tracking-widest text-muted-foreground">Touka</span>}
        </div>
      </div>

      {/* Middle Section: Navigation Buttons (Adaptive) */}
      <div className="flex-1 flex items-center h-full min-w-0 px-2 lg:px-4">
        {!isIDE && !isMobile && (
          <div className="flex-1 flex items-center gap-2 min-w-0 justify-end">
            <div className="flex items-center bg-sidebar/50 p-1 rounded-3xl border border-border/50 gap-0.5 lg:gap-1 overflow-hidden">
              <NavButton mode="chat" activeMode={viewMode} icon={MessageSquare} label={t('layout.main_header.views.chat')} onClick={handleNavClick} />
              <NavButton mode="projects" activeMode={viewMode} icon={Folder} label={t('layout.main_header.views.projects')} onClick={handleNavClick} />
              {sysStatus.mode === 'hybrid' && <NavButton mode="soul" activeMode={viewMode} icon={User} label={t('layout.main_header.views.soul')} onClick={handleNavClick} />}
              <NavButton mode="studio" activeMode={viewMode} icon={Wrench} label={t('layout.main_header.views.studio')} onClick={handleNavClick} />
              {sysStatus.mode === 'hybrid' && (
                <>
                  <NavButton mode="pulse" activeMode={viewMode} icon={Activity} label={t('layout.main_header.views.pulse')} onClick={handleNavClick} />
                  <NavButton mode="vault" activeMode={viewMode} icon={Database} label={t('layout.main_header.views.vault')} onClick={handleNavClick} />
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Right Section: Global Actions */}
      <div className={cn("flex items-center shrink-0", isIDE ? "px-2" : "gap-2 ml-2 lg:ml-4")}>
        <button 
          onClick={() => setViewMode("settings")} 
          className={cn(
            "p-1.5 rounded-lg transition-colors flex-shrink-0", 
            viewMode === "settings" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-sidebar"
          )}
          title={t('layout.main_header.settings_button_title')}
        >
          <Settings size={isIDE ? 16 : 20} />
        </button>
        {!isIDE && (
          <button 
            onClick={onToggleRightSidebar} 
            className={cn(
              "p-1.5 rounded-lg transition-colors flex-shrink-0", 
              isRightSidebarOpen ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-sidebar"
            )}
            title={t('layout.main_header.right_sidebar_title')}
          >
            <BarChart4 size={20} />
          </button>
        )}
      </div>
    </header>
  );
}


