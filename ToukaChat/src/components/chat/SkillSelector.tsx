"use client";

import { useState, useEffect, useCallback } from 'react';
import { Zap, ChevronDown, Check } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { Skill } from '@/lib/types';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import { useChat } from '@/lib/providers/ChatProvider';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { getSystemEpoch } from '@/lib/time_utils';
import { useTranslation } from "react-i18next";

/**
 * SkillSelector — 聊天輸入列的技能啟用切換器（SRP）。
 *
 * 職責：
 *   - 從後端取得可用技能清單（SSOT: /v1/skills/list）
 *   - 讀取當前 session 的 active_skills 並展示啟用狀態
 *   - 使用者勾選/取消技能後，更新 session.active_skills 並呼叫 syncSession 持久化
 *
 * 不負責：技能 CRUD（由 SkillsTab 負責）
 */
export function SkillSelector() {
  const { t } = useTranslation();
  const [availableSkills, setAvailableSkills] = useState<Skill[]>([]);

  const { activeSessionId } = useChat();
  const { sessions, updateSession } = useSessionList();

  const currentSession = sessions.find(s => s.id === activeSessionId);
  const activeSkills: string[] = currentSession?.active_skills ?? [];

  useEffect(() => {
    const proxyUrl = getActiveProxyUrl();
    // P4（ADR-0161）：聊天技能選單排除 is_system 系統技能（三個 dispatcher／coordinator）。
    // include_system=false 為後端 SSOT 過濾；.filter 為前端縱深（防後端未濾／舊版）。
    // MCP 動態掛載改由輸入列 toggle 啟用（system-mcp-dispatcher 不再需出現於此選單）。
    fetchTouka(proxyUrl, '/v1/skills/list?include_system=false')
      .then(async (res) => {
        if (res.ok) {
          const data = await res.json();
          setAvailableSkills((data.skills ?? []).filter((s: Skill) => !s.is_system));
        }
      })
      .catch((e: unknown) => logSSOTError(e, 'SkillSelector:fetchSkills'));
  }, []);

  const handleToggleSkill = useCallback(
    (skillId: string) => {
      if (!activeSessionId) return;
      const updatedAt = getSystemEpoch();
      void updateSession(activeSessionId, session => {
        const current = session.active_skills ?? [];
        const next = current.includes(skillId)
          ? current.filter((s) => s !== skillId)
          : [...current, skillId];
        return { ...session, active_skills: next, updatedAt };
      });
    },
    [activeSessionId, updateSession],
  );

  if (!activeSessionId || availableSkills.length === 0) return null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          title={t('chat.skill_selector.title')}
          className={cn(
            'h-7 px-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider',
            'transition-all rounded-lg border-border/50 bg-background/50',
            activeSkills.length > 0
              ? 'text-primary border-primary/30'
              : 'text-muted-foreground',
          )}
        >
          <Zap size={12} className="shrink-0" />
          <span>{activeSkills.length > 0 ? t('chat.skill_selector.count_label', { n: activeSkills.length }) : t('chat.skill_selector.label')}</span>
          <ChevronDown size={10} className="opacity-50 shrink-0" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-64 p-1 bg-popover/95 backdrop-blur-md border-border shadow-2xl rounded-xl"
      >
        {availableSkills.map((skill) => {
          const isActive = activeSkills.includes(skill.id);
          return (
            <DropdownMenuItem
              key={skill.id}
              onClick={() => handleToggleSkill(skill.id)}
              className={cn(
                'flex flex-col items-start gap-0.5 p-2.5 cursor-pointer rounded-lg mb-0.5 last:mb-0',
                isActive ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50',
              )}
            >
              <div className="flex items-center gap-2 w-full">
                <Zap size={12} className={isActive ? 'text-primary' : 'text-muted-foreground'} />
                <span className="font-bold text-xs flex-1 truncate">{skill.name}</span>
                {isActive && <Check size={12} className="text-primary shrink-0" />}
              </div>
              {skill.description && (
                <span className="text-xs opacity-60 ml-5 leading-tight line-clamp-1">
                  {skill.description}
                </span>
              )}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
