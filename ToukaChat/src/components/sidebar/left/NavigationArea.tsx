"use client";

import React from 'react';
import { 
  MessageCircle, Folder, UserCircle, Wrench, Activity, Database, Settings, LogOut 
} from "lucide-react";
import { cn } from '@/lib/utils';
import { useAuth } from '@/contexts/AuthContext';
import { useTranslation } from "react-i18next";

interface NavItemProps {
  icon: React.ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
  visible?: boolean;
}

function NavItem({ icon, label, active, onClick, visible = true }: NavItemProps) {
  if (!visible) return null;
  return (
    <button
      onClick={onClick}
      // 上導覽列拿掉後側欄是唯一的分頁入口，由這裡告訴螢幕閱讀器目前在哪個分頁
      aria-current={active ? 'page' : undefined}
      // WCAG 1.4.3（A5 axe 實測）：primary 字 on primary/10 wash 為 4.44:1 差一線
      // → wash 降為 /5（4.9:1 ✓），品牌色文字不動
      className={cn(
        "flex items-center gap-3 px-3 py-2 rounded-xl transition-all w-full",
        active ? 'text-primary bg-primary/5' : 'text-muted-foreground hover:text-foreground hover:bg-accent'
      )}
    >
      {icon}
      <span className="text-base font-bold whitespace-nowrap">{label}</span>
    </button>
  );
}

interface NavigationAreaProps {
  activeMode: string;
  isHybrid: boolean;
  onChatClick: () => void;
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
  onChatClick,
  onProjectsClick,
  onSoulClick,
  onStudioClick,
  onPulseClick,
  onVaultClick,
  onSettingsClick
}: NavigationAreaProps) {
  const { t } = useTranslation();
  const [logoutConfirm, setLogoutConfirm] = React.useState(false);
  const { logout } = useAuth();

  const handleLogout = () => {
    if (!logoutConfirm) {
      setLogoutConfirm(true);
      setTimeout(() => setLogoutConfirm(false), 3000);
      return;
    }
    void logout();
  };
  
  // Configuration matrix for navigation (SOLID: OCP)
  const navItems = [
    { id: 'chat', icon: <MessageCircle className="w-5 h-5" />, label: t('sidebar.navigation_area.nav_chat'), onClick: onChatClick, visible: true },
    { id: 'projects', icon: <Folder className="w-5 h-5" />, label: t('sidebar.navigation_area.nav_projects'), onClick: onProjectsClick, visible: true },
    { id: 'soul', icon: <UserCircle className="w-5 h-5" />, label: t('sidebar.navigation_area.nav_soul'), onClick: onSoulClick, visible: isHybrid },
    { id: 'studio', icon: <Wrench className="w-5 h-5" />, label: t('sidebar.navigation_area.nav_studio'), onClick: onStudioClick, visible: true },
    { id: 'pulse', icon: <Activity className="w-5 h-5" />, label: t('sidebar.navigation_area.nav_pulse'), onClick: onPulseClick, visible: isHybrid },
    { id: 'vault', icon: <Database className="w-5 h-5" />, label: t('sidebar.navigation_area.nav_vault'), onClick: onVaultClick, visible: isHybrid },
    { id: 'settings', icon: <Settings className="w-5 h-5" />, label: t('sidebar.navigation_area.nav_settings'), onClick: onSettingsClick, visible: true },
    {
        id: 'logout',
        icon: <LogOut className={cn("w-5 h-5 transition-colors", logoutConfirm ? "text-status-error" : "text-muted-foreground")} />,
        label: logoutConfirm ? t('sidebar.navigation_area.logout_confirm') : t('sidebar.navigation_area.logout'),
        onClick: handleLogout,
        visible: true
    },
  ];

  return (
    <div className="flex flex-col gap-1 border-t border-border pt-3 mt-auto w-full">
      <nav className="flex flex-col gap-0.5">
        {navItems.map((item) => (
          <NavItem 
            key={item.id}
            icon={item.icon}
            label={item.label}
            active={activeMode === item.id}
            onClick={item.onClick}
            visible={item.visible}
          />
        ))}
      </nav>
    </div>
  );
}
