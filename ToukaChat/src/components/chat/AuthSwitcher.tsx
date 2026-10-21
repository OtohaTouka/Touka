"use client";

import React from 'react';
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { Shield, Zap, Rocket, ChevronDown, Lock } from 'lucide-react';
import { useChat } from '@/lib/providers/ChatProvider';
import { useConfirmToast } from '@/hooks/useConfirmToast';
import type { components } from "@/types/api";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

import { Button } from "@/components/ui/button";

// ADR-0246 三模式；第一項 Manual 也是看不懂的值的預設顯示。
const AUTH_LEVELS = [
  { id: 'Manual', name: 'Manual', icon: Shield, color: 'text-primary', descKey: 'chat.auth_switcher.levels.manual_desc' },
  { id: 'Auto', name: 'Auto', icon: Zap, color: 'text-status-warning', descKey: 'chat.auth_switcher.levels.auto_desc' },
  { id: 'YOLO', name: 'YOLO', icon: Rocket, color: 'text-status-error', descKey: 'chat.auth_switcher.levels.yolo_desc' },
] satisfies { id: components['schemas']['SessionDocument']['auth_level']; [key: string]: unknown }[];

export function AuthSwitcher() {
  const { t } = useTranslation();
  const [mounted, setMounted] = React.useState(false);
  const { authLevel, handleSetAuthLevel, activeSessionId, isIncognito } = useChat();
  const { confirmToast } = useConfirmToast();

  React.useEffect(() => {
    setMounted(true);
  }, []);

  // HAX G16（傳達行動後果）：升級到最危險的 YOLO（全自動執行、黑名單與底線除外）
  // 前先二次確認；切換到其他較保守模式則立即套用、不打斷。
  const applyLevel = (id: string) => {
    if (id === 'YOLO' && authLevel !== 'YOLO') {
      confirmToast(
        t('chat.auth_switcher.confirm.yolo_message'),
        () => handleSetAuthLevel(id),
        t('chat.auth_switcher.confirm.yolo_title'),
      );
      return;
    }
    handleSetAuthLevel(id);
  };

  const current = AUTH_LEVELS.find(l => l.id === authLevel) || AUTH_LEVELS[0];
  const Icon = current.icon;

  if (!mounted || !activeSessionId) return null;

  // 無痕對話無法追溯，後端一律以 Manual 執行（U2c-9），選單鎖住、照實顯示。
  if (isIncognito) {
    return (
      <div className="flex items-center gap-1.5">
        <Button
          variant="outline"
          size="sm"
          disabled
          className="h-7 px-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider rounded-lg border-border/50 bg-background/50 text-primary"
        >
          <Lock size={12} className="shrink-0" />
          <span>{AUTH_LEVELS[0].name}</span>
        </Button>
        <span className="text-xs text-muted-foreground">{t('chat.auth_switcher.incognito_manual')}</span>
      </div>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button 
          variant="outline" 
          size="sm" 
          className={cn(
            "h-7 px-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider transition-all rounded-lg border-border/50 bg-background/50",
            current.color
          )}
        >
          <Icon size={12} className="shrink-0" />
          <span>{current.name}</span>
          <ChevronDown size={10} className="opacity-50 shrink-0" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56 p-1 bg-popover/95 backdrop-blur-md border-border shadow-2xl rounded-xl">
        {AUTH_LEVELS.map((level) => {
          const LIcon = level.icon;
          return (
            <DropdownMenuItem
              key={level.id}
              onClick={() => applyLevel(level.id)}
              className={cn(
                "flex flex-col items-start gap-0.5 p-2.5 cursor-pointer rounded-lg mb-0.5 last:mb-0",
                authLevel === level.id ? "bg-accent text-accent-foreground" : "hover:bg-accent/50"
              )}
            >
              <div className="flex items-center gap-2 w-full">
                <LIcon size={14} className={level.color} />
                <span className="font-bold text-xs">{level.name}</span>
                {authLevel === level.id && (
                  <div className="ml-auto w-1 h-1 bg-primary rounded-full" />
                )}
              </div>
              <span className="text-xs opacity-60 ml-5 leading-tight">{t(level.descKey)}</span>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
