"use client";

import React from 'react';
import {
  Folder, UserCircle, Wrench, Activity, Database, Settings, LogOut, Menu
} from "lucide-react";
import { cn } from '@/lib/utils';
import { useAuth } from '@/contexts/AuthContext';
import { useTranslation } from "react-i18next";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/** 按下登出後，確認狀態留多久。 */
const LOGOUT_CONFIRM_MS = 3000;

const MENU_ITEM_CLASS = "flex items-center gap-2 px-2 py-2 text-sm font-bold rounded-lg cursor-pointer transition-colors";

interface NavigationAreaProps {
  activeMode: string;
  isHybrid: boolean;
  onProjectsClick: () => void;
  onSoulClick: () => void;
  onStudioClick: () => void;
  onPulseClick: () => void;
  onVaultClick: () => void;
  onSettingsClick: () => void;
}

export function NavigationArea({
  activeMode,
  isHybrid,
  onProjectsClick,
  onSoulClick,
  onStudioClick,
  onPulseClick,
  onVaultClick,
  onSettingsClick
}: NavigationAreaProps) {
  const { t } = useTranslation();
  const [logoutConfirm, setLogoutConfirm] = React.useState(false);
  const logoutTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const { logout, user } = useAuth();

  const clearLogoutTimer = React.useCallback(() => {
    if (logoutTimer.current !== null) clearTimeout(logoutTimer.current);
    logoutTimer.current = null;
  }, []);

  React.useEffect(() => clearLogoutTimer, [clearLogoutTimer]);

  const handleLogout = (event: Event) => {
    // 確認型選項（§4 UX Sovereignty）：第一下要留在選單裡顯示確認，不能讓選單自動關掉。
    event.preventDefault();
    if (!logoutConfirm) {
      setLogoutConfirm(true);
      logoutTimer.current = setTimeout(() => setLogoutConfirm(false), LOGOUT_CONFIRM_MS);
      return;
    }
    clearLogoutTimer();
    void logout();
  };

  const handleOpenChange = (open: boolean) => {
    if (open) return;
    clearLogoutTimer();
    setLogoutConfirm(false);
  };

  // Configuration matrix for navigation (SOLID: OCP)
  const navItems = [
    { id: 'projects', icon: <Folder className="w-4 h-4" />, label: t('sidebar.navigation_area.nav_projects'), onSelect: onProjectsClick, visible: true },
    { id: 'soul', icon: <UserCircle className="w-4 h-4" />, label: t('sidebar.navigation_area.nav_soul'), onSelect: onSoulClick, visible: isHybrid },
    { id: 'studio', icon: <Wrench className="w-4 h-4" />, label: t('sidebar.navigation_area.nav_studio'), onSelect: onStudioClick, visible: true },
    { id: 'pulse', icon: <Activity className="w-4 h-4" />, label: t('sidebar.navigation_area.nav_pulse'), onSelect: onPulseClick, visible: isHybrid },
    { id: 'vault', icon: <Database className="w-4 h-4" />, label: t('sidebar.navigation_area.nav_vault'), onSelect: onVaultClick, visible: isHybrid },
    { id: 'settings', icon: <Settings className="w-4 h-4" />, label: t('sidebar.navigation_area.nav_settings'), onSelect: onSettingsClick, visible: true },
  ].filter((item) => item.visible);

  const name = user?.display_name || user?.email || t('sidebar.navigation_area.menu');
  // 選單收起來時看不到哪一項是目前畫面，按鈕上要標出來
  const currentPage = navItems.find((item) => item.id === activeMode)?.label;

  return (
    <div className="flex flex-col gap-1 border-t border-border pt-3 mt-auto w-full">
      {/* modal={false}：選了項目會同時關掉手機抽屜，強制模式可能把整頁的點擊留在關閉狀態 */}
      <DropdownMenu modal={false} onOpenChange={handleOpenChange}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={currentPage
              ? t('sidebar.navigation_area.menu_aria_page', { name, page: currentPage })
              : t('sidebar.navigation_area.menu_aria', { name })}
            className="group flex items-center gap-3 px-3 py-2 rounded-xl transition-all w-full text-muted-foreground hover:text-foreground hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            <Menu className="w-5 h-5 shrink-0" />
            <span className="text-base font-bold truncate">{name}</span>
            {/* primary 小字在 accent 上對比不足 4.5:1，滑鼠移上時換色 */}
            {currentPage && <span className="ml-auto text-xs font-bold text-primary group-hover:text-foreground shrink-0">{currentPage}</span>}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="top" align="start" className="z-50 w-56 rounded-xl border border-border bg-popover p-1 shadow-xl">
          {navItems.map((item) => (
            <DropdownMenuItem
              key={item.id}
              onSelect={item.onSelect}
              aria-current={activeMode === item.id ? 'page' : undefined}
              // WCAG 1.4.3（A5 axe 實測）：primary 字 on primary/10 wash 為 4.44:1 差一線
              // → wash 降為 /5（4.9:1 ✓），品牌色文字不動
              className={cn(MENU_ITEM_CLASS, activeMode === item.id ? 'text-primary bg-primary/5' : 'text-muted-foreground hover:text-foreground hover:bg-accent')}
            >
              {item.icon}
              {item.label}
            </DropdownMenuItem>
          ))}
          <DropdownMenuItem
            onSelect={handleLogout}
            className={cn(MENU_ITEM_CLASS, 'text-muted-foreground hover:text-foreground hover:bg-accent')}
          >
            <LogOut className={cn("w-4 h-4 transition-colors", logoutConfirm ? "text-status-error" : "text-muted-foreground")} />
            {logoutConfirm ? t('sidebar.navigation_area.logout_confirm') : t('sidebar.navigation_area.logout')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
